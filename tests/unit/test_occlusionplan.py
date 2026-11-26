"""kritamcp/occlusionplan.py: modes, threshold and the units covering a
target, over a fake node index."""

import pytest

from kritamcp import occlusionplan as op
from kritamcp.errors import CommandError
from kritamcp.refs import NodeIndex


def uid(n):
    return f"{{{n:08x}-0000-4000-8000-{n:012x}}}"


# Top to bottom:
#   Top (paint)
#   PT (pass-through group, multiply) > [PC (paint, clipped), PM (multiply)]
#   Iso (isolated group) > [IA]
#   Vec (vector layer)
#   Hid (hidden paint)
#   G (isolated group) > [GA (paint), Tgt (target), GB (below)]
#   HG (hidden group) > [HA]
#   Bottom
NAMES = ["Top", "PT", "PC", "PM", "Iso", "IA", "Vec", "Hid", "G", "GA", "Tgt", "GB", "HG",
         "HA", "Bottom"]
ID = {name: uid(i + 1) for i, name in enumerate(NAMES)}
PARENT = {"PC": "PT", "PM": "PT", "IA": "Iso", "GA": "G", "Tgt": "G", "GB": "G", "HA": "HG"}
TYPE = {"PT": "grouplayer", "Iso": "grouplayer", "G": "grouplayer", "HG": "grouplayer",
        "Vec": "vectorlayer"}
INDEX = NodeIndex([{"id": ID[n], "name": n, "parent": ID.get(PARENT.get(n)),
                    "type": TYPE.get(n, "paintlayer")} for n in NAMES])


def props(**over):
    out = {}
    for n in NAMES:
        p = {"type": TYPE.get(n, "paintlayer"), "visible": n not in ("Hid", "HG"),
             "blend_mode": "normal", "pass_through": n == "PT", "inherit_alpha": n == "PC"}
        if n in ("PT", "PM"):
            p["blend_mode"] = "multiply"
        p.update(over.get(n, {}))
        out[ID[n]] = p
    return out


def names(ids):
    rev = {v: k for k, v in ID.items()}
    return [rev[i] for i in ids]


def plan(target="Tgt", modes=("normal",), **over):
    return op.plan(INDEX, props(**over), ID[target], op.mode_set(modes))


def test_units_in_global_draw_order():
    p = plan()
    # PT's own mode is ignored (pass-through); Vec isn't a paint layer;
    # Hid is hidden; GB is below the target.
    assert names(p.units) == ["Top", "PC", "Iso", "GA"]
    assert names(p.unclip) == ["PC"]
    assert names(p.neutral) == ["G"]
    assert names(plan(modes=["normal", "multiply"]).units) == ["Top", "PC", "PM", "Iso", "GA"]


def test_isolated_group_counts_by_its_own_mode():
    p = plan(Iso={"blend_mode": "multiply"}, IA={"blend_mode": "multiply"})
    assert "Iso" not in names(p.units) and "IA" not in names(p.units)
    assert "Iso" in names(plan(IA={"blend_mode": "multiply"}).units)


def test_hidden_levels_and_targets():
    # Inside a hidden ancestor, the target's siblings don't count; levels
    # above it still do.
    p = plan(G={"visible": False})
    assert names(p.units) == ["Top", "PC", "Iso"]
    assert names(p.neutral) == []
    # G is isolated: one unit, whatever is inside it.
    assert names(plan("HA").units) == ["Top", "PC", "Iso", "G"]
    assert names(plan("Bottom").units) == ["Top", "PC", "Iso", "G"]
    assert names(plan("Bottom", G={"pass_through": True}).units) == [
        "Top", "PC", "Iso", "GA", "Tgt", "GB"]
    assert plan("Top").units == []


def test_clipped_units_and_pass_through_chain():
    p = plan(PT={"inherit_alpha": True}, Top={"inherit_alpha": True})
    assert sorted(names(p.unclip)) == ["PC", "PT", "Top"]
    # A clipped layer inside an isolated group stays clipped (its base is in the group).
    assert names(plan(IA={"inherit_alpha": True}).unclip) == ["PC"]


def test_modes_param():
    assert op.check_modes(None) == ["normal"]
    assert op.check_modes(["multiply", "normal", "multiply"]) == ["multiply", "normal"]
    for bad in ([], "normal", ["nrmal"], [1], {"normal": 1}):
        with pytest.raises(CommandError) as e:
            op.check_modes(bad)
        assert e.value.code == "bad_request"
    with pytest.raises(CommandError) as e:
        op.check_modes(["normal", "nrmal"])
    assert e.value.details["index"] == 1 and "normal" in e.value.details["suggestions"]
    assert op.mode_set(["add"]) == {"add", "linear_dodge"}
    assert op.mode_set(["linear_dodge", "normal"]) == {"add", "linear_dodge", "normal"}
    assert op.mode_set(["normal"]) == {"normal"}


def test_threshold_param_and_level():
    assert op.check_threshold(None) == 0.5
    assert op.check_threshold(1) == 1.0
    for bad in (0, -0.1, 1.01, True, "0.5", float("nan"), float("inf")):
        with pytest.raises(CommandError) as e:
            op.check_threshold(bad)
        assert e.value.code == "bad_request"
    assert [op.threshold_level(t) for t in (0.5, 1.0, 0.001, 128 / 255, 0.51)] == [
        128, 255, 1, 128, 130]


def test_summary_ids():
    assert op.summary_ids(["a", "b"]) == {"count": 2, "ids": ["a", "b"]}
    assert op.summary_ids([str(i) for i in range(51)]) == {"count": 51}
    assert op.summary_ids([str(i) for i in range(50)])["count"] == 50
