"""Live smoke tests of set_canvas_size (P6.1) and diff_documents (P6.2),
against the GUI Krita through server.py's tools. Run with KRITA_MCP_LIVE=1;
every document is closed via docs_to_close.
"""

import os


def _canvas(call, docs_to_close, work, name):
    doc = call("new_canvas", width=40, height=30, background="transparent", name=name)["doc"]
    docs_to_close.append(doc)
    from PIL import Image
    png = os.path.join(work, f"{name}-part.png")
    Image.new("RGBA", (6, 4), (255, 0, 0, 255)).save(png)
    call("create_layers", doc=doc, specs=[
        {"key": "g", "name": "Group", "type": "group"},
        {"name": "Part", "parent": "$g", "png": png, "x": 2, "y": 3},
    ])
    return doc


def test_set_canvas_size_moves_the_art_and_undoes(call, scratch, docs_to_close, live2d_enabled):
    work, _ = scratch
    doc = _canvas(call, docs_to_close, work, "f001-canvas-size")
    before = call("list_layers", doc=doc)["layers"]

    dry = call("set_canvas_size", doc=doc, width=50, height=36, anchor="center", dry_run=True)
    assert dry["offset"] == {"x": 5, "y": 3} and dry["dry_run"] is True
    assert call("get_document_info", doc=doc)["width"] == 40

    result = call("set_canvas_size", doc=doc, width=50, height=36, anchor="center")
    # F004: the rig warning is a Live2D-convention one.
    assert [w["code"] for w in result["warnings"]] == (["rig_breaking"] if live2d_enabled else [])
    assert ("breaks any existing rig" in result["summary"]) is live2d_enabled
    info = call("get_document_info", doc=doc)
    assert (info["width"], info["height"]) == (50, 36)
    part = {layer["name"]: layer for layer in call("list_layers", doc=doc)["layers"]}["Part"]
    assert part["bounds"] == {"x": 7, "y": 6, "w": 6, "h": 4}
    assert call("get_color_at", doc=doc, x=7, y=6)["color"] == "#ff0000"

    # Shrinking past the art keeps it in the layer; undo brings back the size.
    call("set_canvas_size", doc=doc, width=4, height=4, anchor="top-left")
    part = {layer["name"]: layer for layer in call("list_layers", doc=doc)["layers"]}["Part"]
    assert part["bounds"] == {"x": 7, "y": 6, "w": 6, "h": 4}
    call("undo", doc=doc)
    call("undo", doc=doc)
    assert call("list_layers", doc=doc)["layers"] == before
    assert call("get_document_info", doc=doc)["width"] == 40
    call.error("bad_request", "set_canvas_size", doc=doc, width=4, height=4, anchor="middle")


def test_diff_documents_finds_edits(call, scratch, docs_to_close):
    work, _ = scratch
    doc = _canvas(call, docs_to_close, work, "f001-diff")
    kra = call("save", doc=doc, path=os.path.join(work, "f001-diff-a.kra"))["path"]
    import shutil
    copy = os.path.join(work, "f001-diff-b.kra")
    shutil.copyfile(kra, copy)
    other = call("open_file", path=copy)["doc"]
    docs_to_close.append(other)

    same = call("diff_documents", doc_a=doc, doc_b=other)
    assert same["counts"]["matched"]["uuid"] == same["counts"]["matched"]["total"] > 0
    assert same["projection_changed"] is False and "No differences" in same["summary"]

    call("update_layers", doc=other, changes=[{"id": "Part", "name": "Part 2", "opacity": 0.5}])
    call("set_color", color="#00ff00")  # not the part's red
    call("fill", doc=other, layer="Part 2", x=4, y=4, radius=1)
    diff = call("diff_documents", doc_a=doc, doc_b=other)
    assert [r["name"] for r in diff["renamed"]] == [["Part", "Part 2"]]
    assert diff["changed"][0]["fields"]["opacity"][0] == 1.0
    assert [p["path"] for p in diff["pixel_changes"]] == ["Group/Part 2"]
    assert diff["projection_changed"] is True
    call.error("bad_request", "diff_documents", doc_a=doc, doc_b=doc)
