"""kritamcp.lint: the rules over hand-written snapshots (D14, P4.2; the
conventions set P5.9; speck-only stray-alpha P5.10).

Each rule has a snapshot in tests/unit/snapshots/ with positive and
negative cases. lint_four_findings.json is acceptance test 4's fixture
document (acceptance.md § Fixtures, Lint fixture); conventions_clean.json
is a small generic rig with no finding from any rule.
"""

import collections
import copy
import json
import os

import pytest

from kritamcp import config, lint
from kritamcp.errors import CommandError

SNAPSHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
DEFAULT_RULES = ["node-type", "layer-style", "inherit-alpha", "blend-mode", "duplicate-name",
                 "side-token", "empty-layer", "out-of-canvas", "stray-alpha", "color-space"]
SEVERITY = {"node-type": "error", "layer-style": "error", "inherit-alpha": "error", "blend-mode": "error",
            "duplicate-name": "error", "side-token": "warning", "empty-layer": "warning",
            "out-of-canvas": "warning", "stray-alpha": "warning", "color-space": "error"}
CONVENTION_RULES = ["nesting-depth", "name-case", "blend-suffix", "pose-visibility", "reference-hidden",
                    "mask-suffix"]
CONVENTION_SEVERITY = {"nesting-depth": "warning", "name-case": "info", "blend-suffix": "warning",
                       "pose-visibility": "warning", "reference-hidden": "warning", "mask-suffix": "info"}


def snap(name):
    with open(os.path.join(SNAPSHOTS, name + ".json")) as f:
        return json.load(f)


def ids(snapshot):
    """path -> id (paths are unique in every snapshot here)."""
    return {n["path"]: n["id"] for n in snapshot["nodes"]}


def paths(result):
    return [f["path"] for f in result["findings"]]


def by_path(result):
    return {f["path"]: f for f in result["findings"]}


def run1(snapshot, rule_id, **kw):
    return lint.run(snapshot, [rule_id], **kw)


# --- the snapshot files themselves -------------------------------------

SNAPSHOT_FILES = sorted(f[:-5] for f in os.listdir(SNAPSHOTS) if f.endswith(".json"))


@pytest.mark.parametrize("name", SNAPSHOT_FILES)
def test_snapshot_files_follow_the_format(name):
    s = snap(name)
    assert set(s) <= {"description", "document", "settings", "nodes"}
    # pixel_stats is what the collector adds; the rules don't read it.
    assert set(s["document"]) - {"pixel_stats"} == {"width", "height", "color_model",
                                                    "color_depth", "color_profile"}
    assert set(s["settings"]) == set(lint.DEFAULT_SETTINGS)
    seen = {}
    for i, n in enumerate(s["nodes"]):
        assert n["order"] == i
        parent = seen.get(n["parent"]) if n["parent"] else None
        assert (n["parent"] is None) or parent is not None, "parent must come first"
        assert n["depth"] == (parent["depth"] + 1 if parent else 1)
        assert n["path"] == (f"{parent['path']}/{n['name']}" if parent else n["name"])
        assert (n["alpha"] is not None) == (n["type"] == "paintlayer")
        if n["alpha"] is not None:
            a = n["alpha"]
            assert set(a) == {"pixel_count", "opaque_bounds", "stray_count", "stray_specks"}
            sp = a["stray_specks"]
            base = {"count", "pixels", "regions", "samples", "truncated"}
            assert base <= set(sp) <= base | {"bounds"}  # bounds: F003 K7 collectors
            assert sp["count"] <= sp["pixels"] <= a["stray_count"]
            empty = {"count": 0, "pixels": 0, "regions": 0, "samples": [], "truncated": False}
            assert (a["stray_count"] == 0) <= ({k: v for k, v in sp.items() if k != "bounds"}
                                               == empty)
            assert len(sp["samples"]) == min(sp["count"], 5) or sp["truncated"]
            for r in sp["samples"]:
                assert set(r) == {"x", "y", "w", "h"} and r["w"] >= 1 and r["h"] >= 1
        if n["type"] in lint.MASK_TYPES:
            assert parent and parent["type"] not in lint.MASK_TYPES  # masks hang off a layer
        assert not (n["type"] == "selectionmask" and n["parent"] is None)  # D4: never in a snapshot
        seen[n["id"]] = n


# --- acceptance test 4 -------------------------------------------------

def test_acceptance_four_findings():
    s = snap("lint_four_findings")
    r = lint.run(s)
    assert collections.Counter((f["rule"], f["path"]) for f in r["findings"]) == collections.Counter([
        ("node-type", "Blur"), ("inherit-alpha", "Shade"), ("duplicate-name", "Back/Hair"),
        ("empty-layer", "Spare")])
    i = ids(s)
    dup = by_path(r)["Back/Hair"]
    assert dup["layer_id"] == i["Back/Hair"]
    assert dup["related_ids"] == [i["Head/Hair"], i["Back/Hair"]]
    assert r["counts"] == {"error": 3, "warning": 1, "info": 0}
    assert r["rules_run"] == DEFAULT_RULES
    assert paths(r) == ["Blur", "Back/Hair", "Shade", "Spare"]  # stacking order


def test_acceptance_default_equals_explicit_rule_list():
    s = snap("lint_four_findings")
    assert lint.run(s, DEFAULT_RULES) == lint.run(s) == lint.run(s, ["default"])


# --- registry and selection --------------------------------------------

def test_default_set_is_the_ten_rules_with_their_severities():
    assert lint.set_members("default") == DEFAULT_RULES
    assert {r: lint.RULES[r].severity for r in DEFAULT_RULES} == SEVERITY
    assert set(DEFAULT_RULES) <= set(lint.set_members("all"))
    assert not set(lint.SETS) & set(lint.RULES)


def test_conventions_set_is_the_six_rules_and_all_is_both():
    assert lint.set_members("conventions") == CONVENTION_RULES
    assert {r: lint.RULES[r].severity for r in CONVENTION_RULES} == CONVENTION_SEVERITY
    assert lint.set_members("all") == DEFAULT_RULES + CONVENTION_RULES
    assert not set(DEFAULT_RULES) & set(CONVENTION_RULES)
    assert lint.run(snap("lint_four_findings"), ["default", "conventions"])["rules_run"] == \
        DEFAULT_RULES + CONVENTION_RULES


def test_rule_selection_runs_in_registry_order_without_duplicates():
    s = snap("lint_four_findings")
    r = lint.run(s, ["empty-layer", "node-type", "empty-layer"])
    assert r["rules_run"] == ["node-type", "empty-layer"]
    assert [(f["rule"], f["path"]) for f in r["findings"]] == [("node-type", "Blur"), ("empty-layer", "Spare")]
    assert lint.run(s, ["default", "node-type"])["rules_run"] == DEFAULT_RULES
    assert lint.run(s, ("color-space",)) == {"findings": [], "counts": {"error": 0, "warning": 0, "info": 0},
                                             "rules_run": ["color-space"]}


@pytest.mark.parametrize("rules", [["nodetype"], ["default", "Default"], ["node-type", 3]])
def test_unknown_rule_is_bad_request_listing_valid_names(rules):
    with pytest.raises(CommandError) as e:
        lint.run(snap("lint_four_findings"), rules)
    assert e.value.code == "bad_request"
    assert "valid: default, conventions, all, node-type" in e.value.message
    assert e.value.details["rules"] == list(lint.RULES)
    assert e.value.details["sets"] == ["default", "conventions", "all"]
    assert e.value.details["unknown"] == [n for n in rules if n not in lint.RULES and n not in lint.SETS]


@pytest.mark.parametrize("rules", ["default", [], {"default": 1}])
def test_rules_must_be_a_non_empty_list(rules):
    with pytest.raises(CommandError) as e:
        lint.run(snap("lint_four_findings"), rules)
    assert e.value.code == "bad_request"


def test_bad_snapshot_is_bad_request():
    for bad in ({}, {"nodes": []}, {"document": {}, "nodes": {}}, []):
        with pytest.raises(CommandError) as e:
            lint.run(bad)
        assert e.value.code == "bad_request"


def test_finding_shape():
    r = lint.run(snap("lint_four_findings"))
    for f in r["findings"]:
        want = {"rule", "severity", "layer_id", "path", "message"}
        extra = {"related_ids", "suggested_names"} if f["rule"] == "duplicate-name" else set()
        assert want <= set(f) <= want | extra
        assert f["severity"] == SEVERITY[f["rule"]]
        assert f["path"] in f["message"]


# --- severity_min, counts, sorting -------------------------------------

@pytest.mark.parametrize("floor,want", [
    (None, ["Blur", "Back/Hair", "Shade", "Spare"]),
    ("info", ["Blur", "Back/Hair", "Shade", "Spare"]),
    ("warning", ["Blur", "Back/Hair", "Shade", "Spare"]),
    ("error", ["Blur", "Back/Hair", "Shade"]),
])
def test_severity_min(floor, want):
    r = lint.run(snap("lint_four_findings"), severity_min=floor)
    assert paths(r) == want
    assert r["counts"] == {"error": 3, "warning": 0 if floor == "error" else 1, "info": 0}
    assert r["rules_run"] == DEFAULT_RULES


@pytest.mark.parametrize("bad", ["warn", "ERROR", 1])
def test_bad_severity_min_is_bad_request(bad):
    with pytest.raises(CommandError) as e:
        lint.run(snap("lint_four_findings"), severity_min=bad)
    assert e.value.code == "bad_request"


def test_sorting_document_level_first_then_order_then_rule_id():
    s = snap("lint_four_findings")
    s["document"]["color_depth"] = "U16"
    shade = next(n for n in s["nodes"] if n["name"] == "Shade")
    shade["has_layer_style"] = True
    r = lint.run(s)
    assert [(f["rule"], f["path"]) for f in r["findings"]] == [
        ("color-space", None), ("node-type", "Blur"), ("duplicate-name", "Back/Hair"),
        ("inherit-alpha", "Shade"), ("layer-style", "Shade"), ("empty-layer", "Spare")]


def test_sorting_uses_order_not_list_position():
    s = snap("lint_four_findings")
    want = lint.run(s)
    s["nodes"].reverse()
    assert lint.run(s) == want


# --- node-type ---------------------------------------------------------

def test_node_type():
    r = run1(snap("node_type"), "node-type")
    assert paths(r) == ["Blur", "Fill", "Head/Face/Colorize Mask", "Head/Face Copy", "Head/Lines",
                        "Body/Transparency Mask", "Body/Filter Mask", "Body/Transform Mask",
                        "Body/Selection Mask", "Photo"]
    f = by_path(r)
    assert f["Blur"]["message"] == "Filter layer 'Blur' must be baked or removed"
    assert f["Head/Face Copy"]["message"].startswith("Clone layer ")
    assert f["Photo"]["message"].startswith("File layer ")
    assert f["Body/Transparency Mask"]["message"] == (
        "Transparency mask 'Body/Transparency Mask' on layer 'Body' must be applied or removed")
    assert "on layer 'Head/Face'" in f["Head/Face/Colorize Mask"]["message"]
    assert "on layer 'Body'" in f["Body/Selection Mask"]["message"]  # a local selection is a mask too


def test_node_type_skips_a_root_level_selection_mask():
    # D4: never in a snapshot; if one slips in, it is still not a finding.
    s = snap("lint_four_findings")
    s["nodes"].insert(0, {**s["nodes"][0], "id": "{sel}", "name": "Selection Mask", "path": "Selection Mask",
                          "type": "selectionmask", "order": -1})
    assert [(f["rule"], f["path"]) for f in lint.run(s)["findings"]] == [
        ("node-type", "Blur"), ("duplicate-name", "Back/Hair"), ("inherit-alpha", "Shade"),
        ("empty-layer", "Spare")]


def test_node_type_unknown_type_is_a_finding():
    s = snap("lint_four_findings")
    s["nodes"][6]["type"] = "shapelayer"
    assert "of type 'shapelayer'" in by_path(run1(s, "node-type"))["Body"]["message"]


# --- layer-style, inherit-alpha ----------------------------------------

def test_layer_style():
    r = run1(snap("layer_style"), "layer-style")
    assert paths(r) == ["Head", "Head/Hair"]
    assert "layer style" in r["findings"][0]["message"]


def test_inherit_alpha():
    r = run1(snap("inherit_alpha"), "inherit-alpha")
    assert paths(r) == ["Shade", "Clipped"]
    assert "krita_merge_layers" in r["findings"][0]["message"]


# --- blend-mode (D17) --------------------------------------------------

@pytest.mark.parametrize("mode,extra", [("either", []), ("add", ["Light Add"]), ("linear_dodge", ["Glow Add"])])
def test_blend_mode_additive_modes(mode, extra):
    s = snap("blend_mode")
    s["settings"]["additive_mode"] = mode
    r = run1(s, "blend-mode")
    assert paths(r) == extra + ["Sheen", "Effects/Tint"]  # the screen group and filter layer are exempt
    f = by_path(r)
    assert "'screen'" in f["Sheen"]["message"] and "krita_update_layers" in f["Sheen"]["message"]
    for p in extra:
        other = "linear_dodge" if mode == "add" else "add"
        assert f[p]["message"] == (f"Layer '{p}' uses '{other}', but lint.additive_mode requires '{mode}' "
                                   f"(same rendering): set blend_mode '{mode}' with krita_update_layers")


def test_blend_mode_default_setting_is_either():
    s = snap("blend_mode")
    del s["settings"]
    assert paths(run1(s, "blend-mode")) == ["Sheen", "Effects/Tint"]


def test_bad_additive_mode_in_snapshot_is_bad_request():
    s = snap("blend_mode")
    s["settings"]["additive_mode"] = "Add"
    with pytest.raises(CommandError) as e:
        lint.run(s)
    assert e.value.code == "bad_request"


def test_settings_defaults_match_config():
    assert lint.DEFAULT_SETTINGS == config.DEFAULT_LINT
    assert lint.ADDITIVE_MODES == config.LINT_ADDITIVE_MODES


# --- duplicate-name ----------------------------------------------------

def test_duplicate_name():
    s = snap("duplicate_name")
    i = ids(s)
    r = run1(s, "duplicate-name")
    # Eye's second occurrence (order 3) comes before Hair's (Back/Hair, order 5).
    eye, hair = r["findings"]
    assert eye["layer_id"] == s["nodes"][3]["id"] and eye["path"] == "Head/Eye"
    assert eye["related_ids"] == [s["nodes"][2]["id"], s["nodes"][3]["id"]]
    assert hair["path"] == "Back/Hair"
    assert hair["related_ids"] == [i["Head/Hair"], i["Back/Hair"], i["Hair"]]
    assert hair["message"].startswith(
        "Name 'Hair' is used by 3 paint layers ('Head/Hair', 'Back/Hair', 'Hair'); paint layer "
        "names must be unique (a group may share one): ")
    assert "'Hair 1' / 'Hair 2' / 'Hair 3'" in hair["message"]
    assert "suggested_names" not in hair


def _node(nid, name, kind="paintlayer", parent=None, path=None, order=0):
    return {"id": nid, "name": name, "path": path or name, "type": kind, "parent": parent,
            "order": order, "visible": True}


def _named(nodes):
    """A snapshot of ``nodes``, ``order`` from the list position."""
    for i, n in enumerate(nodes):
        n["order"] = i
    return {"document": {"width": 10, "height": 10, "color_model": "RGBA", "color_depth": "U8",
                         "color_profile": "sRGB-elle-V2-srgbtrc.icc"}, "nodes": nodes}


def test_duplicate_name_group_may_share_a_layer_name():
    """F003 K6: the Face group holding the Face layer is not a clash."""
    s = _named([_node("g1", "Face", "grouplayer"),
                _node("p1", "Face", parent="g1", path="Face/Face"),
                _node("g2", "Neck", "grouplayer"),
                _node("p2", "Neck", parent="g2", path="Neck/Neck")])
    assert run1(s, "duplicate-name")["findings"] == []


def test_duplicate_name_groups_and_layers_each_unique():
    s = _named([_node("g1", "Wand", "grouplayer"),
                _node("g2", "Wand", "grouplayer"),
                _node("p1", "Wand", parent="g2", path="Wand/Wand")])
    (f,) = run1(s, "duplicate-name")["findings"]
    assert f["related_ids"] == ["g1", "g2"]
    assert "used by 2 groups" in f["message"]
    assert "group names must be unique (a layer may share one)" in f["message"]


def test_duplicate_name_suggests_pose_suffix():
    s = _named([_node("a", "Arm L A", "grouplayer"),
                _node("h1", "Hand L", parent="a", path="Arm L A/Hand L"),
                _node("b", "Arm L B", "grouplayer", order=2),
                _node("x", "Fist", "grouplayer", parent="b", path="Arm L B/Fist"),
                _node("h2", "Hand L", parent="x", path="Arm L B/Fist/Hand L")])
    (f,) = run1(s, "duplicate-name")["findings"]
    assert f["message"] == (
        "Name 'Hand L' is used by 2 paint layers ('Arm L A/Hand L', 'Arm L B/Fist/Hand L'); "
        "paint layer names must be unique (a group may share one): rename to 'Hand L A' / "
        "'Hand L B' (pose suffix, CONVENTIONS § Naming)")
    assert f["suggested_names"] == [{"id": "h1", "name": "Hand L A"},
                                    {"id": "h2", "name": "Hand L B"}]


def test_duplicate_name_is_case_sensitive():
    s = snap("lint_four_findings")
    s["nodes"][4]["name"], s["nodes"][4]["path"] = "hair", "Back/hair"
    assert run1(s, "duplicate-name")["findings"] == []


# --- side-token --------------------------------------------------------

def test_side_token():
    s = snap("side_token")
    i = ids(s)
    r = run1(s, "side-token")
    assert paths(r) == ["Head/Eyebrow", "Head/Eye_L", "Head/Ear Left", "Head/Cheek (R)", "Head/Lid l"]
    f = by_path(r)
    assert f["Head/Eyebrow"]["related_ids"] == [i["Head/Eyebrow L"], i["Head/Eyebrow R"]]
    assert "no side token" in f["Head/Eyebrow"]["message"]
    assert "'_L'" in f["Head/Eye_L"]["message"] and "rename it to 'Eye L'" in f["Head/Eye_L"]["message"]
    assert "rename it to 'Ear L'" in f["Head/Ear Left"]["message"]
    assert "rename it to 'Cheek R'" in f["Head/Cheek (R)"]["message"]
    assert "rename it to 'Lid L'" in f["Head/Lid l"]["message"]
    assert "related_ids" not in f["Head/Eye_L"]
    # Body/Eyebrow has no sided sibling in Body; the Arms tree is all standard.


@pytest.mark.parametrize("name,stem,side,odd,suggestion", [
    ("Eyebrow", "Eyebrow", None, [], None),
    ("Eyebrow L", "Eyebrow", "L", [], None),
    ("Arm L A", "Arm A", "L", [], None),
    ("Highlight R 2", "Highlight 2", "R", [], None),
    ("Eyelashes R 1", "Eyelashes 1", "R", [], None),
    ("Eye_L", "Eye", "L", ["_L"], "Eye L"),
    ("Eye_l_2", "Eye 2", "L", ["_l"], "Eye L 2"),
    ("L_Eye", "Eye", "L", ["L_"], "Eye L"),
    ("Hand.r.B", "Hand B", "R", [".r"], "Hand R B"),
    ("Front Hair L-2", "Front Hair 2", "L", ["L-"], "Front Hair L 2"),
    ("Left Hand B", "Hand B", "L", ["Left"], "Hand L B"),
    ("Upper Arm RIGHT", "Upper Arm", "R", ["RIGHT"], "Upper Arm R"),
    ("Hand (R)", "Hand", "R", ["(R)"], "Hand R"),
    ("Hand [l]", "Hand", "L", ["[l]"], "Hand L"),
    ("Eye l", "Eye", "L", ["l"], "Eye L"),
    ("L Eye", "Eye", "L", ["L"], "Eye L"),
    ("L", "L", None, [], None),
    ("Left", "Left", None, [], None),
    ("Lower Lip", "Lower Lip", None, [], None),
    ("Spare", "Spare", None, [], None),
    ("X-Ray", "X-Ray", None, [], None),
    ("[ Guide ]", "[ Guide ]", None, [], None),
])
def test_side_parts(name, stem, side, odd, suggestion):
    assert tuple(lint.side_parts(name)) == (stem, side, odd, suggestion)


def test_side_token_collision_counts_non_standard_tokens_as_sided():
    s = snap("lint_four_findings")
    s["nodes"][7]["name"], s["nodes"][7]["path"] = "Body_L", "Body_L"  # next to top-level Body
    r = run1(s, "side-token")
    assert [(f["path"], "no side token" in f["message"]) for f in r["findings"]] == [
        ("Body", True), ("Body_L", False)]


# --- empty-layer, out-of-canvas, stray-alpha ---------------------------

def test_empty_layer():
    r = run1(snap("empty_layer"), "empty-layer")
    assert paths(r) == ["Spare"]  # the empty group and the filter layer are not paint layers
    assert r["findings"][0]["message"] == "Paint layer 'Spare' has no pixels; paint it or delete it"


def test_out_of_canvas():
    r = run1(snap("out_of_canvas"), "out-of-canvas")
    assert paths(r) == ["Overhang", "Corner", "Top"]
    f = by_path(r)
    assert "outside the 64x64 canvas (3 px left)" in f["Overhang"]["message"]
    assert "(6 px right, 3 px below)" in f["Corner"]["message"]
    assert "(10 px above)" in f["Top"]["message"]


def test_out_of_canvas_exact_fit_is_fine():
    s = snap("out_of_canvas")
    s["document"]["width"] = 70
    s["document"]["height"] = 67
    assert paths(run1(s, "out-of-canvas")) == ["Overhang", "Top"]


def test_stray_alpha_flags_specks_only():
    r = run1(snap("stray_alpha"), "stray-alpha")
    # Glow: 900 stray pixels in one region larger than stray_speck_max_px, no speck.
    assert paths(r) == ["Dust", "Speck", "Glow Dust", "Noise"]
    f = by_path(r)
    assert f["Dust"]["message"] == (
        "Paint layer 'Dust' has 6 stray specks, 37 px (groups of up to 16 px with alpha below 16, "
        "more than 2 px from any pixel with alpha 16 or more), e.g. at (9, 9) 3x4, (15, 10) 5x5, "
        "(20, 12) 2x2; erase them, or clear them with krita_trim_layers(ids=[...], "
        "alpha_threshold=16, near_px=2)")
    assert "has 1 stray speck, 1 px (" in f["Speck"]["message"]
    assert "), at (12, 20) 1x1; erase them" in f["Speck"]["message"]
    assert "2 stray specks, 5 px" in f["Glow Dust"]["message"]
    assert ", at (8, 22) 2x2, (23, 23) 1x1; erase them" in f["Glow Dust"]["message"]
    # K7: pixel count and bounds as data (None from a collector without bounds).
    assert (f["Dust"]["pixels"], f["Dust"]["bounds"]) == (37, None)
    assert "pixels" not in f["Noise"]
    assert f["Noise"]["message"] == (
        "Paint layer 'Noise' has 40000 stray pixels (alpha below 16, more than 2 px from any pixel "
        "with alpha 16 or more), too fragmented to analyse for specks; look for noise and erase it")
    assert all(x["severity"] == "warning" for x in r["findings"])


def test_stray_alpha_reports_speck_bounds():
    s = snap("stray_alpha")
    dust = next(x for x in s["nodes"] if x["path"] == "Dust")
    dust["alpha"]["stray_specks"]["bounds"] = {"x": 9, "y": 9, "w": 30, "h": 12}
    f = by_path(run1(s, "stray-alpha"))["Dust"]
    assert f["bounds"] == {"x": 9, "y": 9, "w": 30, "h": 12}
    assert "(20, 12) 2x2, all within (9, 9) 30x12; erase them" in f["message"]


def test_stray_alpha_without_specks_falls_back_to_stray_count():
    # A snapshot from a collector before P5.10: no stray_specks.
    s = snap("stray_alpha")
    for n in s["nodes"]:
        if n["alpha"]:
            del n["alpha"]["stray_specks"]
    r = run1(s, "stray-alpha")
    assert paths(r) == ["Dust", "Speck", "Glow", "Glow Dust", "Noise"]
    f = by_path(r)
    assert f["Dust"]["message"] == (
        "Paint layer 'Dust' has 37 stray pixels (alpha below 16, more than 2 px from any pixel "
        "with alpha 16 or more); erase them")
    assert "has 1 stray pixel (" in f["Speck"]["message"]


def test_stray_alpha_speck_without_samples():
    s = snap("stray_alpha")
    dust = next(n for n in s["nodes"] if n["name"] == "Dust")
    dust["alpha"]["stray_specks"]["samples"] = []
    msg = by_path(run1(s, "stray-alpha"))["Dust"]["message"]
    assert "alpha 16 or more); erase them, or clear them" in msg


def test_stray_alpha_default_speck_size_in_message():
    s = snap("stray_alpha")
    del s["settings"]["stray_speck_max_px"]
    assert "groups of up to 64 px" in by_path(run1(s, "stray-alpha"))["Dust"]["message"]


# --- color-space -------------------------------------------------------

def test_color_space():
    r = run1(snap("color_space"), "color-space")
    assert len(r["findings"]) == 1
    f = r["findings"][0]
    assert f["layer_id"] is None and f["path"] is None and f["severity"] == "error"
    assert "'GRAYA'" in f["message"] and "'U16'" in f["message"] and "Gray-D50" in f["message"]


@pytest.mark.parametrize("model,depth,profile,ok", [
    ("RGBA", "U8", "sRGB-elle-V2-srgbtrc.icc", True),
    ("RGBA", "U8", "sRGB IEC61966-2.1", True),
    ("RGBA", "U8", "Rec2020-elle-V4-srgbtrc.icc", False),  # sRGB curve, wide gamut: not sRGB
    ("RGBA", "U8", None, False),
    ("RGBA", "U16", "sRGB-elle-V2-srgbtrc.icc", False),
    ("RGBA", "F32", "sRGB-elle-V2-g10.icc", False),
    ("CMYKA", "U8", "sRGB-elle-V2-srgbtrc.icc", False),
])
def test_color_space_cases(model, depth, profile, ok):
    s = snap("lint_four_findings")
    s["document"].update(color_model=model, color_depth=depth, color_profile=profile)
    assert (run1(s, "color-space")["findings"] == []) == ok


# --- reference groups --------------------------------------------------

def test_reference_group_contents_are_skipped_by_every_rule():
    r = lint.run(snap("reference_groups"))
    # Only the bracket group itself (it has a layer style) and a bracket-named
    # paint layer (not a group, so not a reference group).
    assert [(f["rule"], f["path"]) for f in r["findings"]] == [
        ("layer-style", "[ Guide ]"), ("empty-layer", "[ Note ]")]


def test_reference_group_contents_would_fire_otherwise():
    s = snap("reference_groups")
    for n in s["nodes"]:
        n["name"] = n["name"].replace("[", "(").replace("]", ")")
    rules = {f["rule"] for f in lint.run(s)["findings"]}
    assert rules == {"node-type", "layer-style", "inherit-alpha", "blend-mode", "duplicate-name",
                     "side-token", "empty-layer", "out-of-canvas", "stray-alpha"}


@pytest.mark.parametrize("name,ok", [("[ Guide ]", True), ("[Expression References]", True),
                                     (" [ Guide ] ", True), ("[]", True), ("Guide", False),
                                     ("[ Guide", False), ("Guide ]", False), ("(Guide)", False)])
def test_is_reference_group(name, ok):
    assert lint.is_reference_group({"type": "grouplayer", "name": name}) is ok
    assert lint.is_reference_group({"type": "paintlayer", "name": name}) is False


def test_rules_do_not_modify_the_snapshot():
    s = snap("reference_groups")
    before = copy.deepcopy(s)
    lint.run(s, ["all"])
    assert s == before  # rules never modify the snapshot


# --- conventions set (P5.9) --------------------------------------------

@pytest.mark.parametrize("name", SNAPSHOT_FILES)
def test_every_rule_runs_on_every_snapshot(name):
    s = snap(name)
    before = copy.deepcopy(s)
    r = lint.run(s, ["all"])
    assert r["rules_run"] == DEFAULT_RULES + CONVENTION_RULES
    assert s == before
    for f in r["findings"]:
        assert f["severity"] == {**SEVERITY, **CONVENTION_SEVERITY}[f["rule"]]


def test_conventions_clean_rig_has_no_findings():
    assert lint.run(snap("conventions_clean"), ["all"])["findings"] == []


def test_conventions_set_is_opt_in():
    s = snap("conventions_clean")
    s["nodes"][0]["visible"] = True  # [ Guide ]: a reference-hidden finding
    assert lint.run(s)["findings"] == []
    assert [f["rule"] for f in lint.run(s, ["conventions"])["findings"]] == ["reference-hidden"]


def test_nesting_depth():
    r = run1(snap("nesting_depth"), "nesting-depth")
    # Iris L is in exactly 3 groups; the Deep groups, the filter layer and the
    # reference group's 4-deep sketch are exempt.
    assert paths(r) == ["Head/Eyes/Eyeballs/Deep/Pupil L", "Head/Eyes/Eyeballs/Deep/Deeper/Glint L"]
    f = by_path(r)
    assert f["Head/Eyes/Eyeballs/Deep/Pupil L"]["message"] == (
        "Paint layer 'Head/Eyes/Eyeballs/Deep/Pupil L' is 4 groups deep; nest at most 3 "
        "(move it up or merge groups)")
    assert " is 5 groups deep" in f["Head/Eyes/Eyeballs/Deep/Deeper/Glint L"]["message"]


def test_name_case():
    r = run1(snap("name_case"), "name-case")
    assert paths(r) == ["hair shadow", "Eye_L", "Arm R a", "Face  Base", "ヘア", "the End",
                        "Close-up", "front hair"]  # the filter layer and reference contents are exempt
    f = by_path(r)
    assert f["hair shadow"]["message"] == (
        "Layer 'hair shadow' is not Title Case with space-separated words (lowercase 'hair', 'shadow'); "
        "rename it to 'Hair Shadow'")
    assert f["Eye_L"]["message"].endswith("(underscores instead of spaces); rename it to 'Eye L'")
    assert f["Arm R a"]["message"].endswith("rename it to 'Arm R A'")
    assert f["Face  Base"]["message"].endswith("(words not separated by single spaces); rename it to 'Face Base'")
    assert f["ヘア"]["message"].endswith("(not English: 'ヘア')")  # no suggestion
    assert f["the End"]["message"].endswith("rename it to 'The End'")
    assert f["Close-up"]["message"].endswith("rename it to 'Close-Up'")
    assert f["front hair"]["message"].startswith("Group 'front hair' is not Title Case")


@pytest.mark.parametrize("name", [
    "Upper Arm L", "Eyelashes R 1", "Highlight R 2", "Arm L A", "Hand R B", "Back of Head",
    "Hair Shadow Multiply", "Face Shadow Mask", "FX Glow 2", "3D Box", "McKay", "Close-Up",
    "Mother-in-Law", "[ Guide ]", "[ Expression References ]", "[Refs]", "Rock & Roll",
    "Character's Hat", "Eye (Old)", "Layer 2.5",
])
def test_name_case_accepts(name):
    assert lint.name_case_problems(name) == []
    assert lint.title_case(name) is None


@pytest.mark.parametrize("name,problems,fix", [
    ("hair", ["lowercase 'hair'"], "Hair"),
    ("Arm L a", ["lowercase 'a'"], "Arm L A"),            # a minor word, but last: a variant
    ("of Mice", ["lowercase 'of'"], "Of Mice"),           # a minor word, but first
    ("Lid l", ["lowercase 'l'"], "Lid L"),
    ("eye_l", ["underscores instead of spaces", "lowercase 'eye', 'l'"], "Eye L"),
    (" Hair", ["leading or trailing space"], "Hair"),
    ("Hair\tBack", ["words not separated by single spaces"], "Hair Back"),
    ("[ guide ]", ["lowercase 'guide'"], "[ Guide ]"),
    ("x2", ["lowercase 'x2'"], "X2"),
    ("ヘア L", ["not English: 'ヘア'"], None),
    ("", ["empty name"], None),
    ("   ", ["empty name"], None),
])
def test_name_case_rejects(name, problems, fix):
    assert lint.name_case_problems(name) == problems
    assert lint.title_case(name) == fix


def test_blend_suffix():
    r = run1(snap("blend_suffix"), "blend-suffix")
    # Hair Shadow Multiply, Glow Add and Light Add (linear_dodge, D17) are right;
    # the screen layer has no suffix (blend-mode's finding); groups are exempt.
    assert paths(r) == ["Face Shadow", "Sparkle", "Cheek Multiply", "Rim Add", "Shine Multiply",
                        "Tint multiply"]
    f = by_path(r)
    assert f["Face Shadow"]["message"] == (
        "Layer 'Face Shadow' uses blend mode 'multiply' but its name doesn't end in ' Multiply'; "
        "rename it to 'Face Shadow Multiply'")
    assert f["Sparkle"]["message"].endswith("doesn't end in ' Add'; rename it to 'Sparkle Add'")
    assert f["Cheek Multiply"]["message"] == (
        "Layer 'Cheek Multiply' ends in ' Multiply' but uses blend mode 'normal'; set blend_mode "
        "'multiply' with krita_update_layers, or rename it to 'Cheek'")
    assert "set blend_mode 'add' with krita_update_layers, or rename it to 'Rim'" in f["Rim Add"]["message"]
    assert f["Shine Multiply"]["message"] == (
        "Layer 'Shine Multiply' uses blend mode 'add' but its name ends in ' Multiply'; rename it to "
        "'Shine Add' or change its blend mode with krita_update_layers")
    assert f["Tint multiply"]["message"] == (
        "Layer 'Tint multiply' ends in ' multiply'; spell the suffix ' Multiply': rename it to "
        "'Tint Multiply'")


def test_blend_suffix_suggests_the_required_additive_spelling():
    s = snap("blend_suffix")
    s["settings"]["additive_mode"] = "linear_dodge"
    assert "set blend_mode 'linear_dodge'" in by_path(run1(s, "blend-suffix"))["Rim Add"]["message"]


def test_blend_suffix_one_word_name_has_no_suffix():
    s = snap("blend_suffix")
    plain = next(n for n in s["nodes"] if n["name"] == "Plain")
    plain["name"] = plain["path"] = "Add"
    assert "Add" not in paths(run1(s, "blend-suffix"))
    plain["blend_mode"] = "add"
    assert by_path(run1(s, "blend-suffix"))["Add"]["message"].endswith("rename it to 'Add Add'")


def test_pose_visibility():
    s = snap("pose_visibility")
    r = run1(s, "pose-visibility")
    # Arm L B is hidden (right); Arm R B hidden (right) but Arm R A is hidden too.
    # Hand L A/B are paint layers, Eye L/R side tokens, Mouth B/C have no A,
    # Hat A is alone, Tail A and Tail B have different parents.
    assert paths(r) == ["Arms/Arm L C", "Arms/Arm R A"]
    i = ids(s)
    f = by_path(r)
    assert f["Arms/Arm L C"]["message"] == (
        "Pose group 'Arms/Arm L C' is visible; hide it (only pose A, 'Arm L A', stays visible)")
    assert f["Arms/Arm L C"]["related_ids"] == [i["Arms/Arm L A"], i["Arms/Arm L B"]]
    assert f["Arms/Arm R A"]["message"] == (
        "Pose group 'Arms/Arm R A' is hidden; keep pose A visible and the others hidden")
    assert f["Arms/Arm R A"]["related_ids"] == [i["Arms/Arm R B"]]


@pytest.mark.parametrize("name,parts", [("Arm L A", ("Arm L", "A")), ("Hat B", ("Hat", "B")),
                                        ("Arm L", None), ("Arm R", None), ("A", None), ("Arm a", None),
                                        ("Arm L AB", None), ("Mouth Z", ("Mouth", "Z"))])
def test_pose_parts(name, parts):
    assert lint.pose_parts(name) == parts


def test_reference_hidden():
    r = run1(snap("reference_hidden"), "reference-hidden")
    # [Refs] is hidden; [ Inner ] is inside a reference group (skipped);
    # [ Note ] is a paint layer. A hidden parent doesn't excuse [ Sketch ].
    assert paths(r) == ["[ Guide ]", "Extras/[ Sketch ]"]
    assert r["findings"][0]["message"] == (
        "Reference group '[ Guide ]' is visible; hide it (reference material stays hidden)")


def test_mask_suffix():
    s = snap("mask_suffix")
    r = run1(s, "mask-suffix")
    # Freckles clips to Cheek Mask (has the suffix); Bottom Shine has nothing
    # below it; the transparency mask is not a sibling; reference contents skipped.
    assert paths(r) == ["Face/Skin", "Body Base"]
    i = ids(s)
    f = by_path(r)
    assert f["Face/Skin"]["message"] == (
        "Layer 'Face/Skin' is the clipping base of 'Blush', 'Shading'; if it exists only to clip, "
        "rename it to 'Skin Mask'")
    assert f["Face/Skin"]["related_ids"] == [i["Face/Blush"], i["Face/Shading"]]
    assert f["Body Base"]["message"].startswith("Group 'Body Base' is the clipping base of 'Clothes'")
    assert lint.clipping_bases(s) == {i["Face/Skin"]: [i["Face/Blush"], i["Face/Shading"]],
                                      i["Face/Cheek Mask"]: [i["Face/Freckles"]],
                                      i["Body Base"]: [i["Clothes"]]}


def test_mask_suffix_fires_only_with_inherit_alpha():
    s = snap("mask_suffix")
    for n in s["nodes"]:
        n["inherit_alpha"] = False
    assert lint.run(s, ["mask-suffix", "inherit-alpha"])["findings"] == []
    r = lint.run(snap("mask_suffix"), ["mask-suffix", "inherit-alpha"])
    clipped = {c for f in r["findings"] if f["rule"] == "mask-suffix" for c in f["related_ids"]}
    flagged = {f["layer_id"] for f in r["findings"] if f["rule"] == "inherit-alpha"}
    assert clipped <= flagged
