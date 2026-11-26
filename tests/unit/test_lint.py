"""kritamcp.lint: the default rules over hand-written snapshots (D14, P4.2).

Each rule has a snapshot in tests/unit/snapshots/ with positive and
negative cases. lint_four_findings.json is acceptance test 4's fixture
document (acceptance.md § Fixtures, Lint fixture).
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
        assert set(f) == (want | {"related_ids"} if f["rule"] == "duplicate-name" else want)
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
    assert "'Hair' is used by 3 nodes ('Head/Hair', 'Back/Hair', 'Hair')" in hair["message"]


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


def test_stray_alpha():
    r = run1(snap("stray_alpha"), "stray-alpha")
    assert paths(r) == ["Dust", "Speck"]
    f = by_path(r)
    assert f["Dust"]["message"] == (
        "Paint layer 'Dust' has 37 stray pixels (alpha below 16, more than 2 px from any pixel "
        "with alpha 16 or more); erase them")
    assert "has 1 stray pixel (" in f["Speck"]["message"]


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
