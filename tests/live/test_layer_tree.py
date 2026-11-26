"""Live smoke test of the layer-tree tools (P2.5), against the GUI Krita
through server.py's tools: create, list, update, move, delete on a small
canvas; and export_layers. Run with KRITA_MCP_LIVE=1; the canvases are
closed via docs_to_close.
"""

import json
import os


def _tree(call, doc):
    """{name: layer object} and the top-level names, top to bottom."""
    layers = call("list_layers", doc=doc)["layers"]
    by_name = {l["name"]: l for l in layers}
    top = [l["name"] for l in layers if l["parent"] is None]
    return by_name, top


def test_create_list_update_move_delete(call, docs_to_close):
    doc = call("new_canvas", width=32, height=32, name="f001-layer-tree")["doc"]
    docs_to_close.append(doc)

    created = call("create_layers", doc=doc, specs=[
        {"name": "A"},
        {"key": "g", "name": "G", "type": "group"},
        {"name": "X", "parent": "$g"},
        {"name": "Y", "parent": "$g"},
    ])
    ids = {c["name"]: c["id"] for c in created["created"]}
    by_name, top = _tree(call, doc)
    assert top == ["G", "A", "Background"]
    assert by_name["G"]["children"] == [ids["Y"], ids["X"]]

    # update: properties and a duplicate-name warning.
    updated = call("update_layers", doc=doc, changes=[
        {"id": "A", "opacity": 0.5, "blend_mode": "multiply", "visible": False},
        {"id": "G", "pass_through": True},
        {"id": "G/Y", "name": "A"}])
    assert [u["id"] for u in updated["updated"]] == [ids["A"], ids["G"], ids["Y"]]
    assert [w["code"] for w in updated["warnings"]] == ["duplicate_name"]
    assert "warning" in updated["summary"]
    by_name, _ = _tree(call, doc)
    layer = next(l for l in call("list_layers", doc=doc)["layers"] if l["id"] == ids["A"])
    assert (layer["opacity"], layer["blend_mode"], layer["visible"]) == (0.502, "multiply", False)
    assert by_name["G"]["pass_through"] is True
    call.error("bad_request", "update_layers", doc=doc,
               changes=[{"id": ids["A"], "pass_through": True}])

    # move: a group to the bottom, a layer out of it, a bad cycle.
    moved = call("move_layers", doc=doc, moves=[
        {"id": "G", "index": 0},
        {"id": "X", "parent": "/", "above": ids["A"]}])
    assert moved["moved"] == [
        {"id": ids["G"], "path": "G", "parent": "/", "index": 0},
        {"id": ids["X"], "path": "X", "parent": "/", "index": 3}]
    by_name, top = _tree(call, doc)
    assert top == ["X", "A", "Background", "G"]
    assert by_name["G"]["children"] == [ids["Y"]]  # the group kept the rest of its subtree
    call.error("bad_request", "move_layers", doc=doc, moves=[{"id": "G", "parent": "G"}])
    dry = call("move_layers", doc=doc, moves=[{"id": "X", "index": 0}], dry_run=True)
    assert "dry run" in dry["summary"]
    assert _tree(call, doc)[1] == top

    # delete: the group goes with its subtree.
    deleted = call("delete_layers", doc=doc, ids=["G", ids["Y"]])
    assert deleted["deleted"] == [ids["G"], ids["Y"]] and deleted["count"] == 2
    _, top = _tree(call, doc)
    assert top == ["X", "A", "Background"]
    call.error("bad_request", "delete_layers", doc=doc, ids=["/"])
    call.error("not_found", "delete_layers", doc=doc, ids=["No Such Layer"])


def test_export_layers(call, docs_to_close, scratch):
    """export_layers (P3.2): PNGs and a manifest that create_layers rebuilds."""
    from PIL import Image

    work, _ = scratch
    src = os.path.join(work, "export-src.png")
    image = Image.new("RGBA", (10, 8), (0, 0, 0, 0))
    for x in range(2, 7):
        for y in range(1, 5):
            image.putpixel((x, y), (200, 40, 10, 128 + x))
    image.save(src)

    doc = call("new_canvas", width=32, height=32, background="transparent",
               name="f001-export")["doc"]
    docs_to_close.append(doc)
    call("create_layers", doc=doc, specs=[
        {"key": "g", "name": "G", "type": "group", "pass_through": True},
        {"name": "Ink", "parent": "$g", "png": src, "x": 5, "y": 6, "blend_mode": "multiply",
         "opacity": 0.5},
        {"name": "Spare", "parent": "$g"}])

    out = call("export_layers", doc=doc, dir=os.path.join(work, "export"), png_level=1)
    assert "Exported 1 PNG" in out["summary"]
    by_path = {e["path"]: e for e in out["layers"]}
    assert [e["path"] for e in out["layers"]] == ["Background", "G", "G/Ink", "G/Spare"]
    ink = by_path["G/Ink"]
    assert (ink["x"], ink["y"], ink["w"], ink["h"]) == (7, 7, 5, 4)  # cropped to alpha > 0
    assert (ink["blend_mode"], round(ink["opacity"] * 255)) == ("multiply", 128)
    assert by_path["G"]["pass_through"] is True and by_path["G/Spare"]["png"] is None
    with Image.open(os.path.join(out["dir"], ink["png"])) as im:
        assert im.convert("RGBA").tobytes() == image.crop((2, 1, 7, 5)).tobytes()
    with open(out["manifest"], encoding="utf-8") as f:
        assert json.load(f)["layers"] == out["layers"]
    call.error("conflict", "export_layers", doc=doc, dir=out["dir"])

    doc2 = call("new_canvas", width=32, height=32, background="transparent",
                name="f001-export-2")["doc"]
    docs_to_close.append(doc2)
    rebuilt = call("create_layers", doc=doc2, manifest=out["manifest"])
    assert rebuilt["counts"]["total"] == 4
    keep = ("path", "type", "visible", "opacity", "blend_mode", "pass_through", "bounds")
    tree = [{k: l[k] for k in keep} for l in call("list_layers", doc=doc)["layers"]]
    tree2 = [{k: l[k] for k in keep} for l in call("list_layers", doc=doc2)["layers"]]
    assert tree2[:-1] == tree  # doc2 also has its own (empty) Background at the bottom
