"""Live smoke tests of transform_layer (P5.6) and merge_layers (P5.7), against
the GUI Krita through server.py's tools. Run with KRITA_MCP_LIVE=1; the
canvases are closed via docs_to_close.
"""

import os


def _png(work, name, size, fill):
    from PIL import Image
    path = os.path.join(work, name)
    Image.new("RGBA", size, fill).save(path)
    return path


def test_transform_flip_translate_and_undo(call, scratch, docs_to_close):
    work, _ = scratch
    doc = call("new_canvas", width=64, height=48, background="transparent",
               name="f001-transform")["doc"]
    docs_to_close.append(doc)
    from PIL import Image
    png = os.path.join(work, "transform-art.png")
    art = Image.new("RGBA", (8, 4), (0, 0, 255, 255))
    art.putpixel((0, 0), (255, 0, 0, 255))  # a red marker at the top-left
    art.save(png)
    call("create_layers", doc=doc, specs=[{"name": "Art", "png": png, "x": 10, "y": 6}])

    flipped = call("transform_layer", doc=doc, id="Art", flip="h")
    assert flipped["bounds_after"] == {"x": 10, "y": 6, "w": 8, "h": 4}
    assert call("get_color_at", x=17, y=6, doc=doc, layer="Art")["color"].lower() == "#ff0000"
    moved = call("transform_layer", doc=doc, id="Art", translate=[5, 3])
    assert moved["bounds_after"] == {"x": 15, "y": 9, "w": 8, "h": 4}
    assert call("get_color_at", x=22, y=9, doc=doc, layer="Art")["color"].lower() == "#ff0000"
    turned = call("transform_layer", doc=doc, id="Art", rotate=90)
    assert turned["bounds_after"]["w"] == 4 and turned["bounds_after"]["h"] == 8

    for _ in range(3):
        call("undo", doc=doc)
    listed = [l for l in call("list_layers", doc=doc)["layers"] if l["name"] == "Art"]
    assert listed[0]["bounds"] == {"x": 10, "y": 6, "w": 8, "h": 4}
    assert call("get_color_at", x=10, y=6, doc=doc, layer="Art")["color"].lower() == "#ff0000"
    call.error("bad_request", "transform_layer", doc=doc, id="Art")


def test_merge_group_flatten_render_unchanged_and_undo(call, scratch, docs_to_close):
    work, _ = scratch
    doc = call("new_canvas", width=64, height=48, background="#ffffff",
               name="f001-merge")["doc"]
    docs_to_close.append(doc)
    base = _png(work, "merge-base.png", (30, 20), (0, 128, 255, 200))
    clip = _png(work, "merge-clip.png", (40, 40), (255, 0, 0, 255))
    shade = _png(work, "merge-shade.png", (20, 20), (128, 128, 128, 255))
    created = call("create_layers", doc=doc, specs=[
        {"key": "g", "name": "Part", "type": "group", "opacity": 0.8},
        {"name": "Base", "parent": "$g", "png": base, "x": 10, "y": 10},
        {"name": "Clip", "parent": "$g", "png": clip, "inherit_alpha": True},
        {"name": "Shade", "parent": "$g", "png": shade, "x": 20, "y": 15,
         "blend_mode": "multiply"},
    ])
    ids = {c["name"]: c["id"] for c in created["created"]}
    before = call("render", doc=doc, path=os.path.join(work, "merge-before.png"))["path"]

    merged = call("merge_layers", doc=doc, ids=["Part"], name="Part flat")
    assert merged["mode"] == "group" and set(merged["removed"]) == set(ids.values())
    assert merged["layer"]["opacity"] == 0.8
    after = call("render", doc=doc, path=os.path.join(work, "merge-after.png"))["path"]
    from PIL import Image
    assert list(Image.open(before).getdata()) == list(Image.open(after).getdata())

    call("undo", doc=doc)
    listed = {l["id"]: l for l in call("list_layers", doc=doc)["layers"]}
    assert set(ids.values()) <= set(listed) and merged["layer"]["id"] not in listed
    undone = call("render", doc=doc, path=os.path.join(work, "merge-undone.png"))["path"]
    assert list(Image.open(before).getdata()) == list(Image.open(undone).getdata())
