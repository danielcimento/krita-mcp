"""The ``live2d`` lint pack (F004 survey.md, category 3): the Live2D rig
conventions of ``live2d-workspace/CONVENTIONS.md``. Pure: stdlib only.

Opt-in: the rules here are in the sets only while ``settings.live2d.enabled``
(the config's ``live2d.enabled``) is true, and only then does the pack
make ``duplicate-name`` an error and suggest pose-suffix renames for it.
A rule named by id runs anyway, with the pack's settings.

Settings (``settings.live2d``, from the config's ``live2d`` section):

- ``additive_mode``: which spelling of the rig's Add is accepted:
  ``either`` (default), ``add`` or ``linear_dodge`` (D17).
- ``allowed_blend_modes``: the paint-layer blend modes the rig takes
  (default normal, multiply, add, linear_dodge: the legacy Cubism modes;
  CONVENTIONS § Layer types).
- ``max_nesting``: groups above a paint layer (default 3,
  ``Head/Eyes/Eyeballs``; CONVENTIONS § Structure).
- ``pose_pattern``: a regular expression with groups ``stem`` and
  ``pose`` that a pose group's name matches in full (default: the last
  word one capital other than the side tokens L and R, ``Arm L A``), and
  ``default_pose``: the pose kept visible (default ``A``). Pose groups are
  sibling groups with the same stem, two or more (CONVENTIONS
  § Structure, umamo v3 U11).

Settled here (api-design.md § Lint rules, P4.2 note):

- **Side tokens** (CONVENTIONS § Naming): the standard token is a separate
  word ``L`` or ``R``, after the first word, placed before trailing
  variant/index words (``Arm L A``, ``Highlight R 2``). See ``side_parts``.
- The ``default`` set: node-type, layer-style, inherit-alpha, blend-mode,
  side-token, color-space. The ``conventions`` set (opt-in even with the
  pack on): nesting-depth, name-case, blend-suffix, pose-visibility,
  reference-hidden, reference-unbracketed, mask-suffix.
"""

import re
from typing import NamedTuple

from .engine import (GROUP, MASK_TYPES, PAINT, TYPE_LABELS, Pack, checked_nodes, compiled,
                     finding, is_reference_group, register_pack, rule, settings)

LIVE2D = "live2d"
ADDITIVE_MODES = ("either", "add", "linear_dodge")
ADDITIVE = ("add", "linear_dodge")  # one mode, two IDs (D17)
POSE_PATTERN = r"^(?P<stem>.*\S) (?P<pose>[A-KM-QS-Z])$"
DEFAULTS = {"enabled": False, "additive_mode": "either",
            "allowed_blend_modes": ["normal", "multiply", "add", "linear_dodge"],
            "max_nesting": 3, "pose_pattern": POSE_PATTERN, "default_pose": "A"}


def _pack(snapshot):
    return settings(snapshot)[LIVE2D]


# --- pose groups ----------------------------------------------------------

def pose_parts(name, pattern=None):
    """``(stem, letter)`` for a pose name (``pattern``, default
    ``POSE_PATTERN``: ``<X> A``, the last word one capital other than the
    side tokens ``L`` / ``R``), else None."""
    m = compiled(pattern or POSE_PATTERN).fullmatch(name)
    return (m.group("stem"), m.group("pose")) if m else None


def pose_families(nodes, pattern, types=(GROUP,)):
    """``{(parent, type, stem): [(letter, node)]}`` over ``nodes`` of
    ``types``, in stacking order. A family of two or more is a pose group
    set."""
    families = {}
    for n in nodes:
        if n.get("type") in types:
            p = pose_parts(n.get("name") or "", pattern)
            if p:
                families.setdefault((n.get("parent"), n.get("type"), p[0]), []).append((p[1], n))
    return families


def _pose_letter(node, by_id, pattern):
    """The pose letter of the nearest enclosing pose group of ``node``, or None."""
    parent = by_id.get(node.get("parent"))
    while parent is not None:
        p = pose_parts(parent.get("name") or "", pattern) if parent.get("type") == GROUP else None
        if p:
            return p[1]
        parent = by_id.get(parent.get("parent"))
    return None


def suggest_renames(name, nodes, by_id, s):
    """``duplicate-name``'s suggestion while the pack is on: when each copy
    sits in a different pose group, the pose suffix (``Hand L A`` /
    ``Hand L B``, CONVENTIONS § Naming), as data too. Else None."""
    pattern = s[LIVE2D]["pose_pattern"]
    letters = [_pose_letter(n, by_id, pattern) for n in nodes]
    if not (all(letters) and len(set(letters)) == len(letters)):
        return None
    names = [f"{name} {letter}" for letter in letters]
    shown = " / ".join(f"'{n}'" for n in names)
    return (f"rename to {shown} (pose suffix, CONVENTIONS § Naming)",
            [{"id": n["id"], "name": new} for n, new in zip(nodes, names)])


def check_settings(p):
    """Problems with the pack's settings ``p`` (snapshot validation)."""
    problems = []
    if p.get("additive_mode") not in ADDITIVE_MODES:
        problems.append(f"additive_mode must be one of {', '.join(ADDITIVE_MODES)}, "
                        f"got {p.get('additive_mode')!r}")
    modes = p.get("allowed_blend_modes")
    if not isinstance(modes, (list, tuple)) or not modes or not all(isinstance(m, str) for m in modes):
        problems.append(f"allowed_blend_modes must be a non-empty list of blend mode ids, got {modes!r}")
    depth = p.get("max_nesting")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        problems.append(f"max_nesting must be a positive whole number, got {depth!r}")
    try:
        groups = compiled(p.get("pose_pattern")).groupindex
        if not {"stem", "pose"} <= set(groups):
            problems.append("pose_pattern needs the groups (?P<stem>...) and (?P<pose>...)")
    except (TypeError, re.error) as e:
        problems.append(f"pose_pattern is not a regular expression ({e})")
    if not isinstance(p.get("default_pose"), str) or not p.get("default_pose"):
        problems.append(f"default_pose must be a non-empty string, got {p.get('default_pose')!r}")
    return problems


register_pack(Pack(LIVE2D, "Live2D rig conventions", DEFAULTS,
                   severity={"duplicate-name": "error"},
                   suggest_renames=suggest_renames, check=check_settings))


# --- default set ------------------------------------------------------------

@rule("node-type", "error", pack=LIVE2D)
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


@rule("layer-style", "error", pack=LIVE2D)
def layer_style(snapshot):
    return [finding("layer-style", n, f"Layer '{n.get('path')}' has a layer style; "
                                      "bake it into the pixels or remove it")
            for n in checked_nodes(snapshot) if n.get("has_layer_style")]


@rule("inherit-alpha", "error", pack=LIVE2D)
def inherit_alpha(snapshot):
    return [finding("inherit-alpha", n, f"Layer '{n.get('path')}' inherits alpha (clipping); bake it "
                                        "into the layer it clips to with krita_merge_layers")
            for n in checked_nodes(snapshot) if n.get("inherit_alpha")]


def _modes_text(modes):
    """"normal, multiply and add" (add and linear_dodge are one mode, D17)."""
    shown = []
    for m in modes:
        m = "add" if m in ADDITIVE else m
        if m not in shown:
            shown.append(m)
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


@rule("blend-mode", "error", pack=LIVE2D)
def blend_mode(snapshot):
    p = _pack(snapshot)
    want, allowed = p["additive_mode"], list(p["allowed_blend_modes"])
    if any(m in ADDITIVE for m in allowed):
        allowed += [m for m in ADDITIVE if m not in allowed]  # either spelling; additive_mode picks
    out = []
    for n in checked_nodes(snapshot):
        if n.get("type") != PAINT:
            continue  # group blending doesn't matter to the rig
        mode = n.get("blend_mode") or "normal"
        if mode not in allowed:
            out.append(finding("blend-mode", n, (
                f"Layer '{n.get('path')}' uses blend mode '{mode}'; the rig supports only "
                f"{_modes_text(allowed)}: bake it, or change it with krita_update_layers")))
        elif mode in ADDITIVE and want != "either" and mode != want:
            out.append(finding("blend-mode", n, (
                f"Layer '{n.get('path')}' uses '{mode}', but live2d.additive_mode requires '{want}' "
                f"(same rendering): set blend_mode '{want}' with krita_update_layers")))
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


@rule("side-token", "warning", pack=LIVE2D)
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


@rule("color-space", "error", pack=LIVE2D)
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

@rule("nesting-depth", "warning", sets=("conventions",), pack=LIVE2D)
def nesting_depth(snapshot):
    """A paint layer inside more than ``max_nesting`` groups."""
    limit = _pack(snapshot)["max_nesting"]
    out = []
    for n in checked_nodes(snapshot):
        groups = (n.get("depth") or 1) - 1
        if n.get("type") == PAINT and groups > limit:
            out.append(finding("nesting-depth", n, (
                f"Paint layer '{n.get('path')}' is {groups} groups deep; nest at most {limit} "
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


@rule("name-case", "info", sets=("conventions",), pack=LIVE2D)
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


@rule("blend-suffix", "warning", sets=("conventions",), pack=LIVE2D)
def blend_suffix(snapshot):
    """A paint layer in Multiply or Add (``add`` or ``linear_dodge``, D17)
    whose last word isn't ``Multiply`` / ``Add``, or whose last word is one
    of them (any case) but the mode doesn't match. A one-word name has no
    suffix. Groups are exempt (group blending doesn't matter to the rig)."""
    want_add = _pack(snapshot)["additive_mode"]
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


@rule("pose-visibility", "warning", sets=("conventions",), pack=LIVE2D)
def pose_visibility(snapshot):
    """Sibling pose groups (two or more, one of them the default pose ``A``):
    the default pose hidden, or another pose visible. Own ``visible``
    flags only (siblings share their ancestors). A family without the
    default pose is not checked."""
    p = _pack(snapshot)
    first = p["default_pose"]
    out = []
    for members in pose_families(checked_nodes(snapshot), p["pose_pattern"]).values():
        if len(members) < 2 or not any(letter == first for letter, _ in members):
            continue
        a_name = next(n.get("name") for letter, n in members if letter == first)
        for letter, n in members:
            others = [m["id"] for _, m in members if m is not n]
            if letter == first and not n.get("visible", True):
                out.append(finding("pose-visibility", n, (
                    f"Pose group '{n.get('path')}' is hidden; keep pose {first} visible and the others "
                    "hidden"), others))
            elif letter != first and n.get("visible", True):
                out.append(finding("pose-visibility", n, (
                    f"Pose group '{n.get('path')}' is visible; hide it (only pose {first}, '{a_name}', "
                    "stays visible)"), others))
    return out


@rule("reference-hidden", "warning", sets=("conventions",), pack=LIVE2D)
def reference_hidden(snapshot):
    """A reference group (``is_reference_group``) whose own ``visible`` is
    true. Its contents stay skipped."""
    s = settings(snapshot)
    return [finding("reference-hidden", n, (
                f"Reference group '{n.get('path')}' is visible; hide it (reference material "
                "stays hidden)"))
            for n in checked_nodes(snapshot) if is_reference_group(n, s) and n.get("visible", True)]


def bracketed_name(name):
    """The reference spelling of ``name``: ``[ Expression References ]``."""
    return f"[ {name.strip()} ]"


@rule("reference-unbracketed", "warning", sets=("conventions",), pack=LIVE2D)
def reference_unbracketed(snapshot):
    """F004 K8. A hidden group (own ``visible`` false) that isn't a
    reference group and isn't a pose group (``pose_families``: a sibling
    group shares its stem), and a hidden top-level paint layer that isn't
    a pose variant among its sibling layers. Reference material belongs in
    a bracketed group (CONVENTIONS § Naming); hidden rig parts are
    legitimate, so this is a warning that names both readings. Nodes inside
    a flagged group are not flagged again. A group's finding carries
    ``suggested_names`` (the bracketed name, when the reference pattern
    accepts it); a layer's fix is a move into such a group, so it has none.
    """
    s = settings(snapshot)
    pattern = s[LIVE2D]["pose_pattern"]
    nodes = checked_nodes(snapshot)
    families = pose_families(nodes, pattern, types=(GROUP, PAINT))

    def pose_variant(n):
        p = pose_parts(n.get("name") or "", pattern)
        return bool(p) and len(families.get((n.get("parent"), n.get("type"), p[0]), ())) >= 2

    inside, out = set(), []
    for n in nodes:
        if n.get("parent") in inside:
            inside.add(n["id"])
            continue
        if n.get("visible", True) or pose_variant(n):
            continue
        name, path = n.get("name") or "", n.get("path")
        if n.get("type") == GROUP and not is_reference_group(n, s):
            new = bracketed_name(name)
            f = finding("reference-unbracketed", n, (
                f"Hidden group '{path}' isn't bracketed: if it's reference material, rename it to "
                f"'{new}'; if it's a rig part that starts hidden, ignore this warning"))
            if is_reference_group({"type": GROUP, "name": new}, s):
                f["suggested_names"] = [{"id": n["id"], "name": new}]
            out.append(f)
            inside.add(n["id"])
        elif n.get("type") == PAINT and n.get("parent") is None:
            new = bracketed_name(name.strip().strip("[]") or name)
            out.append(finding("reference-unbracketed", n, (
                f"Hidden layer '{path}' isn't in a bracketed group: if it's reference material, move "
                f"it into a hidden group named '{new}'; if it's a rig part that starts hidden, ignore "
                "this warning")))
    return out


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


@rule("mask-suffix", "info", sets=("conventions",), pack=LIVE2D)
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
