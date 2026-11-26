"""Live smoke test of select (P5.4) and selection_to_layer (P5.5), against the
GUI Krita through server.py's tools. Run with KRITA_MCP_LIVE=1; the canvas
is closed via docs_to_close.
"""

import os


def test_polygon_cut_to_layer_and_undo(call, scratch, docs_to_close):
    work, _ = scratch
    doc = call("new_canvas", width=64, height=48, background="transparent",
               name="f001-selection")["doc"]
    docs_to_close.append(doc)
    from PIL import Image
    png = os.path.join(work, "selection-art.png")
    Image.new("RGBA", (64, 48), (255, 0, 0, 255)).save(png)
    call("create_layers", doc=doc, specs=[{"name": "Art", "png": png}])
    assert call("get_color_at", x=10, y=10, doc=doc, layer="Art")["a"] == 255

    # A triangle on pixel corners, no antialiasing: (10, 10) is inside, (50, 40) outside.
    sel = call("select", doc=doc, mode="polygon", points=[[4, 4], [40, 4], [4, 40]],
               antialias=False)
    assert sel["pixel_count"] > 0 and sel["bounds"]["x"] == 4
    assert call("get_document_info", doc=doc)["selection"] == sel["bounds"]
    layer_types = [l["type"] for l in call("list_layers", doc=doc)["layers"]]
    assert "selectionmask" not in layer_types

    cut = call("selection_to_layer", doc=doc, source="Art", name="Part")
    assert cut["cut"] is True and cut["copied"] == sel["pixel_count"]
    part = cut["layer"]["id"]
    top = [l["name"] for l in call("list_layers", doc=doc)["layers"] if l["parent"] is None]
    assert top[:2] == ["Part", "Art"]
    assert call("get_color_at", x=10, y=10, doc=doc, layer="Art")["a"] == 0
    assert call("get_color_at", x=10, y=10, doc=doc, layer=part)["color"].lower() == "#ff0000"
    assert call("get_color_at", x=50, y=40, doc=doc, layer=part)["a"] == 0
    assert call("get_color_at", x=50, y=40, doc=doc, layer="Art")["a"] == 255
    # The composite looks the same as before the cut.
    rendered = call("render", doc=doc, path=os.path.join(work, "selection-cut.png"))
    assert os.path.getsize(rendered["path"]) > 0
    assert call("get_color_at", x=10, y=10, doc=doc)["color"].lower() == "#ff0000"

    call("undo", doc=doc)
    names = [l["name"] for l in call("list_layers", doc=doc)["layers"]]
    assert "Part" not in names
    assert call("get_color_at", x=10, y=10, doc=doc, layer="Art")["a"] == 255

    cleared = call("select", doc=doc, mode="none")
    assert (cleared["bounds"], cleared["pixel_count"]) == (None, 0)
    assert call("get_document_info", doc=doc)["selection"] is None
    call.error("conflict", "selection_to_layer", doc=doc, source="Art", name="Again")
