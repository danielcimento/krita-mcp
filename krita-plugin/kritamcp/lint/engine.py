"""The lint engine (D14, F004 G2): rule registry, packs, sets, settings and
``run``. Pure: stdlib only, no krita / PyQt5.

``krita_lint`` collects a snapshot inside Krita (``commands/analysis.py``)
and hands it to ``run``. The snapshot format is in api-design.md
§ Snapshot; in short::

    {"document": {"width", "height", "color_model", "color_depth", "color_profile"},
     "settings": {"stray_alpha_below", "stray_alpha_distance", "stray_speck_max_px",
                  "reference_pattern", "disable",
                  "live2d": {"enabled", "additive_mode", "allowed_blend_modes",
                             "max_nesting", "pose_pattern", "default_pose"}},
     "nodes": [{"id", "name", "path", "type", "parent", "depth", "order",
                "visible", "opacity", "blend_mode", "inherit_alpha", "alpha_locked",
                "locked", "pass_through", "has_layer_style", "bounds",
                "alpha": {"pixel_count", "opaque_bounds", "stray_count",
                          "stray_specks": {...}} | None}, ...]}

``nodes`` is every node except the root-level selection mask, in global
stacking order top to bottom; ``order`` is the position in that list.
``settings`` may be partial: missing keys take ``default_settings()``.

**Rules and packs** (F004 survey.md). Every rule belongs to a pack:

- ``core`` (``lint/core.py``): problems for any agent working in Krita.
  Always available; the config's ``lint.disable`` takes a rule out of the
  sets (category 2, opt-out).
- ``live2d`` (``lint/live2d.py``): the Live2D rig conventions of
  ``live2d-workspace/CONVENTIONS.md``. Its rules are in the sets only when
  ``settings.live2d.enabled`` (the config's ``live2d.enabled``) is true
  (category 3, opt-in). A pack can also raise a core rule's severity and
  suggest renames for ``duplicate-name`` while it is on.

**Sets.** ``default``, ``conventions`` (the opt-in extras of a pack) and
``all`` expand to their *enabled* members; a rule a set would have
included but the config leaves out is reported in ``rules_off``. A rule
named by id always runs, whatever the config says: that is an explicit
choice by the caller.

Each rule is ``fn(snapshot) -> [finding]``. A finding is ``{rule,
severity, layer_id, path, message}`` plus ``related_ids`` when several
nodes are involved, and ``fix`` when the way to fix it is the same text
for every finding of the rule (F005 K14: ``message`` then says only what
is wrong, and ``krita_lint`` gives ``fix`` once per rule group);
document-level findings have ``layer_id`` and ``path`` ``None``. ``run`` sets each finding's ``severity`` to the rule's
effective severity (``severity_of``).

**Reference groups**: a ``grouplayer`` whose name, trimmed, matches
``settings.reference_pattern`` (default: wrapped in brackets, ``[ Guide ]``,
``[Refs]``; ``None`` turns it off). Everything inside one, at any depth,
is skipped by every rule. The group itself is checked like any other.
Only groups count: a bracket-named paint layer is linted like any other.
"""

import functools
import re
from typing import NamedTuple

from ..errors import CommandError

SEVERITIES = ("info", "warning", "error")
SETS = ("default", "conventions", "all")  # "all" = every enabled rule
CORE = "core"

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

# The core settings. Packs add theirs under their name (``default_settings``).
REFERENCE_PATTERN = r"^\[.*\]$"
CORE_SETTINGS = {"stray_alpha_below": 8, "stray_alpha_distance": 4, "stray_speck_max_px": 64,
                 "reference_pattern": REFERENCE_PATTERN, "disable": []}


class Rule(NamedTuple):
    id: str
    severity: str  # with no pack on; a pack may raise it (Pack.severity)
    sets: tuple
    pack: str
    fn: object


class Pack(NamedTuple):
    """An opt-in rule pack. ``defaults``: its settings, under
    ``settings[name]`` (``enabled`` False). ``severity``: rule id ->
    severity while the pack is on. ``suggest_renames``: ``fn(name, nodes,
    by_id, settings) -> (text, [{id, name}] | None) | None`` for
    ``duplicate-name``, tried while the pack is on. ``check``:
    ``fn(pack_settings) -> [problem]`` for snapshot validation."""
    name: str
    title: str
    defaults: dict
    severity: dict = {}
    suggest_renames: object = None
    check: object = None


RULES = {}  # id -> Rule, in registration order
PACKS = {}  # name -> Pack


def rule(rule_id, severity, sets=("default",), pack=CORE):
    """Register ``fn(snapshot) -> [finding]`` as rule ``rule_id``."""
    assert severity in SEVERITIES and rule_id not in RULES and rule_id not in SETS
    assert all(s in SETS and s != "all" for s in sets)

    def register(fn):
        RULES[rule_id] = Rule(rule_id, severity, tuple(sets), pack, fn)
        return fn
    return register


def register_pack(pack):
    assert pack.name not in PACKS and pack.name != CORE and pack.name not in CORE_SETTINGS
    assert "enabled" in pack.defaults
    PACKS[pack.name] = pack


def set_members(name):
    """The rule ids in set ``name``, in registration order, whatever the
    settings (``run`` drops the ones they leave out)."""
    return [r.id for r in RULES.values() if name == "all" or name in r.sets]


# --- settings -------------------------------------------------------------

def default_settings():
    """The full default settings: the core ones, and each pack's under its
    name (off). A fresh copy."""
    out = {k: list(v) if isinstance(v, list) else v for k, v in CORE_SETTINGS.items()}
    for p in PACKS.values():
        out[p.name] = {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in p.defaults.items()}
    return out


def settings(snapshot):
    """The snapshot's ``settings`` over ``default_settings()`` (one level
    deep for packs)."""
    given = snapshot.get("settings") or {}
    out = default_settings()
    for key, value in given.items():
        if key in PACKS and isinstance(value, dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def pack_on(s, name):
    """Whether pack ``name`` is enabled in settings ``s`` (core always is)."""
    return name == CORE or bool((s.get(name) or {}).get("enabled"))


def rule_enabled(r, s):
    """Whether the sets include rule ``r`` under settings ``s``."""
    return pack_on(s, r.pack) and r.id not in (s.get("disable") or ())


def severity_of(rule_id, s):
    """Rule ``rule_id``'s severity under settings ``s``: the most severe
    override of an enabled pack, else its own."""
    severity = RULES[rule_id].severity
    for p in PACKS.values():
        override = p.severity.get(rule_id)
        if override and pack_on(s, p.name) and SEVERITIES.index(override) > SEVERITIES.index(severity):
            severity = override
    return severity


@functools.lru_cache(maxsize=32)
def compiled(pattern):
    return re.compile(pattern)


# --- run ----------------------------------------------------------------

def run(snapshot, rules=None, severity_min=None):
    """Run ``rules`` (rule ids and/or set names; ``["default"]`` if None) on
    ``snapshot``. Returns ``{findings, counts, rules_run, rules_off}``.

    ``rules_off`` is ``{reason: [rule ids]}``: the rules a requested set
    left out because of the settings, reason ``"disabled"`` (``disable``)
    or a pack name (that pack is off). ``findings`` are sorted by the
    involved node's ``order`` (document-level first), then by rule id.
    ``severity_min`` drops less severe findings; ``counts`` counts the
    findings returned. Unknown names, a bad ``severity_min`` or bad
    ``settings`` are ``bad_request``.
    """
    check_args(rules, severity_min)
    _check_snapshot(snapshot)
    s = settings(snapshot)
    selected, off = select(["default"] if rules is None else rules, s)
    floor = SEVERITIES.index(severity_min or "info")
    order = {n["id"]: n.get("order", i) for i, n in enumerate(snapshot["nodes"])}

    findings = []
    for rule_id in selected:
        severity = severity_of(rule_id, s)
        if SEVERITIES.index(severity) < floor:
            continue
        for f in RULES[rule_id].fn(snapshot):
            f["severity"] = severity
            findings.append(f)
    findings.sort(key=lambda f: (f["layer_id"] is not None, order.get(f["layer_id"], 0), f["rule"]))
    counts = {s: 0 for s in ("error", "warning", "info")}
    for f in findings:
        counts[f["severity"]] += 1
    return {"findings": findings, "counts": counts, "rules_run": selected, "rules_off": off}


def check_args(rules=None, severity_min=None):
    """Validate ``run``'s ``rules`` and ``severity_min`` without a snapshot
    (``krita_lint`` checks them before collecting one). Returns the names
    given (``["default"]`` for None); bad values are ``bad_request``."""
    names = ["default"] if rules is None else rules
    if isinstance(names, str) or not isinstance(names, (list, tuple)) or not names:
        raise CommandError("bad_request", "rules must be a non-empty list of rule ids and/or set names")
    valid = list(SETS) + list(RULES)
    unknown = [n for n in names if not isinstance(n, str) or n not in valid]
    if unknown:
        raise CommandError("bad_request",
                           f"unknown lint rule or set {', '.join(repr(n) for n in unknown)}; "
                           f"valid: {', '.join(valid)}",
                           {"unknown": unknown, "sets": list(SETS), "rules": list(RULES)})
    if severity_min is not None and severity_min not in SEVERITIES:
        raise CommandError("bad_request", f"severity_min must be one of {', '.join(SEVERITIES)}, "
                                          f"got {severity_min!r}")
    return list(names)


def select(names, s):
    """``(rule ids to run, rules_off)`` for valid ``names`` under settings
    ``s``: sets expand to their enabled members, rule ids are kept as is."""
    wanted, off = set(), {}
    for n in names:
        if n not in SETS:
            wanted.add(n)
            continue
        for rid in set_members(n):
            r = RULES[rid]
            if rule_enabled(r, s):
                wanted.add(rid)
            else:
                off.setdefault(rid, r.pack if not pack_on(s, r.pack) else "disabled")
    by_reason = {}
    for rid in RULES:  # registry order
        if rid in off and rid not in wanted:
            by_reason.setdefault(off[rid], []).append(rid)
    return [r for r in RULES if r in wanted], by_reason


def _check_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), list) \
            or not isinstance(snapshot.get("document"), dict):
        raise CommandError("bad_request", "lint snapshot must have a 'document' object and a 'nodes' list")
    s = settings(snapshot)
    problems = []
    pattern = s.get("reference_pattern")
    if pattern is not None:
        try:
            compiled(pattern)
        except (TypeError, re.error) as e:
            problems.append(f"reference_pattern {pattern!r} is not a regular expression ({e})")
    disable = s.get("disable")
    if not isinstance(disable, (list, tuple)) or not all(isinstance(d, str) for d in disable):
        problems.append(f"disable must be a list of rule ids, got {disable!r}")
    for p in PACKS.values():
        if not isinstance(s.get(p.name), dict):
            problems.append(f"{p.name} must be an object, got {s.get(p.name)!r}")
        elif p.check:
            problems += [f"{p.name}.{msg}" for msg in p.check(s[p.name])]
    if problems:
        raise CommandError("bad_request", "bad lint settings: " + "; ".join(problems))


# --- snapshot helpers ---------------------------------------------------

def is_reference_group(node, s=None):
    """A group whose trimmed name matches the reference pattern (settings
    ``s``; default: wrapped in brackets, CONVENTIONS § Naming)."""
    pattern = (s or CORE_SETTINGS).get("reference_pattern")
    if pattern is None or node.get("type") != GROUP:
        return False
    return compiled(pattern).search((node.get("name") or "").strip()) is not None


def stacked(snapshot):
    """``nodes`` sorted by ``order`` (they normally already are)."""
    nodes = snapshot["nodes"]
    return [n for _, n in sorted(enumerate(nodes), key=lambda p: (p[1].get("order", p[0]), p[0]))]


def checked_nodes(snapshot):
    """The nodes the rules look at, in stacking order: all of them except the
    contents of reference groups and a stray root-level selection mask."""
    s = settings(snapshot)
    skip = set()
    out = []
    for n in stacked(snapshot):  # parents come before their children (depth-first, top to bottom)
        if n.get("parent") in skip:
            skip.add(n["id"])
            continue
        if n.get("type") == "selectionmask" and n.get("parent") is None:
            continue  # the document's global selection (D4); never in a snapshot, but be safe
        if is_reference_group(n, s):
            skip.add(n["id"])
        out.append(n)
    return out


def finding(rule_id, node, message, related_ids=None, fix=None):
    """A finding. ``fix``: how to fix it, when that text doesn't depend on
    the node (F005 K14); ``message`` then says only what is wrong."""
    f = {"rule": rule_id, "severity": RULES[rule_id].severity,
         "layer_id": None if node is None else node["id"],
         "path": None if node is None else node.get("path"), "message": message}
    if related_ids is not None:
        f["related_ids"] = list(related_ids)
    if fix is not None:
        f["fix"] = fix
    return f


def alpha(node):
    return node.get("alpha") if node.get("type") == PAINT else None


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"
