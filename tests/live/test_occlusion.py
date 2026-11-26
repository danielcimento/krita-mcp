"""Live smoke test of occlusion_map (P5.8), against the GUI Krita through
server.py's tools. Run with KRITA_MCP_LIVE=1; the canvas is closed via
docs_to_close.
"""

import os


def test_occlusion_of_a_covered_layer(call, scratch, docs_to_close):
    work, _ = scratch
    doc = call("new_canvas", width=64, height=48, background="transparent",
               name="f001-occlusion")["doc"]
    docs_to_close.append(doc)
    from PIL import Image
    under = os.path.join(work, "occl-under.png")
    cover = os.path.join(work, "occl-cover.png")
    Image.new("RGBA", (20, 10), (255, 0, 0, 255)).save(under)
    Image.new("RGBA", (10, 10), (0, 0, 255, 255)).save(cover)
    created = call("create_layers", doc=doc, specs=[
        {"name": "Group", "type": "group"},
        {"name": "Under", "png": under, "x": 4, "y": 6},
        {"name": "Cover", "png": cover, "x": 4, "y": 6},
        {"name": "Tint", "png": under, "x": 4, "y": 6, "blend_mode": "multiply"},
    ])["created"]
    ids = {c["name"]: c["id"] for c in created}
    before = call("list_layers", doc=doc)["layers"]

    path = os.path.join(work, "occl-map.png")
    result = call("occlusion_map", doc=doc, id="Under", path=path)
    assert result["covered_pct"] == 0.5 and result["fully_hidden"] is False
    assert result["above"] == {"count": 1, "ids": [ids["Cover"]]}
    assert result["bounds"] == {"x": 4, "y": 6, "w": 20, "h": 10}
    image = Image.open(result["path"])
    assert (image.mode, image.size) == ("L", (20, 10))
    assert image.getpixel((0, 0)) == 255 and image.getpixel((15, 5)) == 0

    full = call("occlusion_map", doc=doc, id="Under", path=path,
                modes=["normal", "multiply"])
    assert full["covered_pct"] == 1.0 and full["fully_hidden"] is True
    assert "fully hidden" in full["summary"]

    # Reads only: the tree (visibility included) is unchanged.
    assert call("list_layers", doc=doc)["layers"] == before
    call.error("conflict", "occlusion_map", doc=doc, id="Group", path=path)
