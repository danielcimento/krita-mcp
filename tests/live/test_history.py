"""Live test of the agent journal (P5.1, D6), against the GUI Krita through
server.py's tools: create, update, move, delete and import_png, then
krita_undo five times restores the document (list_layers and render
compared with the start), krita_redo five times reproduces the end state,
and krita_ui_undo / krita_ui_redo return their warning. Run with
KRITA_MCP_LIVE=1; the canvas is closed via docs_to_close.
"""

import os

W, H = 32, 24


def _pattern_png(path, w, h, seed):
    """An RGBA PNG with varied colour and alpha."""
    from PIL import Image
    data = bytes((i * 37 + seed * 11 + (i // 4) * 5) % 256 for i in range(4 * w * h))
    Image.frombytes("RGBA", (w, h), data).save(path)
    return path


def _render_bytes(path):
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        return im.mode, im.size, im.tobytes()


def _state(call, doc, work, name):
    """(list_layers layers, rendered RGBA bytes) of ``doc``."""
    layers = call("list_layers", doc=doc)["layers"]
    rendered = call("render", doc=doc, path=os.path.join(work, f"{name}.png"), png_level=1)
    return layers, _render_bytes(rendered["path"])


def test_undo_five_tools_then_redo(call, scratch, docs_to_close):
    work = os.path.join(scratch[0], "history")
    os.makedirs(work, exist_ok=True)
    doc = call("new_canvas", width=W, height=H, name="f001-history")["doc"]
    docs_to_close.append(doc)
    call("create_layers", doc=doc, specs=[
        {"name": "P", "png": _pattern_png(os.path.join(work, "p.png"), W, H, 1)},
        {"key": "g", "name": "G", "type": "group"},
        {"name": "Q", "parent": "$g", "png": _pattern_png(os.path.join(work, "q.png"), 9, 7, 2),
         "x": 5, "y": 3, "blend_mode": "multiply"},
        {"key": "h", "name": "H", "type": "group", "parent": "$g"},
        {"name": "Y", "parent": "$h", "png": _pattern_png(os.path.join(work, "y.png"), 12, 6, 3),
         "x": -3, "y": 10},
    ])
    start = _state(call, doc, work, "start")

    call("create_layers", doc=doc, specs=[
        {"name": "N1"},
        {"key": "g2", "name": "G2", "type": "group"},
        {"name": "N2", "parent": "$g2"}])
    call("update_layers", doc=doc, changes=[
        {"id": "P", "name": "P2", "opacity": 0.5},
        {"id": "G", "pass_through": True, "blend_mode": "screen"}])
    call("move_layers", doc=doc, moves=[
        {"id": "N1", "parent": "G", "index": 0},
        {"id": "P2", "index": 0}])
    call("delete_layers", doc=doc, ids=["G"])
    imported = call("import_png", doc=doc, id="G2/N2",
                    png=_pattern_png(os.path.join(work, "n2.png"), 6, 5, 9), x=20, y=2)
    assert imported["warnings"] == []
    end = _state(call, doc, work, "end")
    assert end != start

    journal = call("journal", doc=doc)
    assert [r["label"] for r in journal["undo"][:5]] == [
        "import_png", "delete_layers", "move_layers", "update_layers", "create_layers"]

    labels = []
    for _ in range(5):
        result = call("undo", doc=doc)
        labels.append(result["undone"]["label"])
        assert result["warnings"] == []
        assert "Undid" in result["summary"] and doc in result["summary"]
    assert labels == ["import_png", "delete_layers", "move_layers", "update_layers",
                      "create_layers"]
    assert result["remaining"]["redo"] == 5
    assert _state(call, doc, work, "undone") == start

    for _ in range(5):
        result = call("redo", doc=doc)
    assert result["redone"]["label"] == "import_png"
    assert result["remaining"]["redo"] == 0
    assert _state(call, doc, work, "redone") == end
    call.error("conflict", "redo", doc=doc)


def test_ui_undo_warns(call, docs_to_close):
    doc = call("new_canvas", width=16, height=16, name="f001-ui-undo")["doc"]
    docs_to_close.append(doc)
    call("create_layers", doc=doc, specs=[{"name": "A"}])  # a step on Krita's own stack
    for tool in ("ui_undo", "ui_redo"):
        result = call(tool, doc=doc)
        assert result["doc"] == doc
        assert [w["code"] for w in result["warnings"]] == ["partial_ui_undo"]
        assert "only partly covers agent edits" in result["warnings"][0]["message"]
        assert "warning" in result["summary"]
