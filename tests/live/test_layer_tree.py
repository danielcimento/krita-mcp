"""Live smoke test of the layer-tree tools (P2.5), against the GUI Krita
through server.py's tools: create, list, update, move, delete on a small
canvas. Run with KRITA_MCP_LIVE=1; the canvas is closed via docs_to_close.
"""


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
