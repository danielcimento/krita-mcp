"""Live smoke tests of set_active_layer, duplicate_layer (P5.2), layer_stats
and trim_layers (P5.3), against the GUI Krita through server.py's tools.
Run with KRITA_MCP_LIVE=1; the canvases are closed via docs_to_close.
"""

import os


def _png(path, w, h, pixels):
    """An RGBA PNG, transparent but for ``pixels`` ({(x, y): (r, g, b, a)})."""
    from PIL import Image
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for xy, rgba in pixels.items():
        image.putpixel(xy, rgba)
    image.save(path)
    return path


def _stats(call, doc, ref):
    return call("layer_stats", doc=doc, ids=[ref])["layers"][0]


def test_set_active_layer_steers_painting(call, docs_to_close):
    """In the GUI the new canvas is the active document, so painting with
    neither doc nor layer lands on the layer set_active_layer picked."""
    doc = call("new_canvas", width=64, height=64, background="transparent",
               name="f001-active-layer")["doc"]
    docs_to_close.append(doc)
    ids = {c["name"]: c["id"] for c in call("create_layers", doc=doc, specs=[
        {"name": "A"}, {"name": "B"}])["created"]}
    listed = call("list_documents")["documents"]
    assert next(d for d in listed if d["doc"] == doc)["active"] is True

    result = call("set_active_layer", doc=doc, id="A")
    assert (result["layer"], result["warnings"]) == (ids["A"], [])
    call("set_color", color="#ff0000")
    call("fill", x=32, y=32, radius=10)  # no doc, no layer: the active document's active layer
    assert _stats(call, doc, "A")["pixel_count"] > 0
    assert _stats(call, doc, "B")["pixel_count"] == 0

    call("set_active_layer", id="B")  # no doc: the active document
    call("fill", x=10, y=10, radius=4)
    assert _stats(call, doc, "B")["pixel_count"] > 0
    call.error("not_found", "set_active_layer", doc=doc, id="No Such Layer")


def test_set_active_layer_warns_for_another_document(call, docs_to_close):
    first = call("new_canvas", width=16, height=16, name="f001-active-1")["doc"]
    docs_to_close.append(first)
    second = call("new_canvas", width=16, height=16, name="f001-active-2")["doc"]  # now active
    docs_to_close.append(second)
    call("create_layers", doc=first, specs=[{"name": "A"}])
    result = call("set_active_layer", doc=first, id="A")
    assert [w["code"] for w in result["warnings"]] == ["not_active_document"]


def test_duplicate_layer(call, scratch, docs_to_close):
    work, _ = scratch
    src = _png(os.path.join(work, "dup.png"), 8, 6, {(x, y): (10, 200, 30, 255)
                                                     for x in range(2, 6) for y in range(1, 4)})
    doc = call("new_canvas", width=32, height=32, name="f001-duplicate")["doc"]
    docs_to_close.append(doc)
    ids = {c["name"]: c["id"] for c in call("create_layers", doc=doc, specs=[
        {"key": "g", "name": "G", "type": "group"},
        {"name": "X", "parent": "$g", "png": src, "x": 3, "y": 4, "blend_mode": "multiply"}],
    )["created"]}

    copy = call("duplicate_layer", doc=doc, id="G")
    assert copy["layer"]["name"] == "G" and copy["layer"]["id"] != ids["G"]
    assert len(copy["ids"]) == 2
    assert [w["code"] for w in copy["warnings"]] == ["duplicate_name", "duplicate_names_in_copy"]
    top = [l["name"] for l in call("list_layers", doc=doc)["layers"] if l["parent"] is None]
    assert top == ["G", "G", "Background"]

    # Two groups are named G now, so the path "G/X" is ambiguous (D4): use the id.
    call.error("ambiguous", "duplicate_layer", doc=doc, id="G/X")
    renamed = call("duplicate_layer", doc=doc, id=ids["X"], name="X2", parent=copy["layer"]["id"])
    assert (renamed["layer"]["name"], renamed["layer"]["parent"]) == ("X2", copy["layer"]["id"])
    assert _stats(call, doc, renamed["layer"]["id"])["opaque_bounds"] == {
        "x": 5, "y": 5, "w": 4, "h": 3}
    dry = call("duplicate_layer", doc=doc, id=ids["X"], dry_run=True)
    assert "dry run" in dry["summary"]

    call("undo", doc=doc)
    call("undo", doc=doc)
    top = [l["name"] for l in call("list_layers", doc=doc)["layers"] if l["parent"] is None]
    assert top == ["G", "Background"]


def test_layer_stats_and_trim(call, scratch, docs_to_close):
    work, _ = scratch
    pixels = {(x, y): (0, 0, 255, 255) for x in range(4, 10) for y in range(4, 10)}
    pixels[(10, 5)] = (0, 0, 255, 3)    # anti-aliased edge
    pixels[(25, 18)] = (0, 255, 0, 5)   # faint speck far away
    src = _png(os.path.join(work, "trim.png"), 32, 24, pixels)
    doc = call("new_canvas", width=32, height=24, background="transparent",
               name="f001-trim")["doc"]
    docs_to_close.append(doc)
    call("create_layers", doc=doc, specs=[{"name": "L", "png": src},
                                          {"name": "G", "type": "group"}])

    stats = call("layer_stats", doc=doc, ids=["L", "G"])
    layer = stats["layers"][0]
    assert (layer["pixel_count"], layer["opaque_count"]) == (38, 36)
    assert layer["alpha_histogram"]["counts"][1] == 2  # alpha 1-7
    assert layer["beyond_canvas"] is None and layer["stray_specks"]["count"] == 1
    assert [s["type"] for s in stats["skipped"]] == ["grouplayer"]

    dry = call("trim_layers", doc=doc, ids=["L"], alpha_threshold=8, near_px=2, dry_run=True)
    assert dry["totals"]["cleared"] == 1 and "dry run" in dry["summary"]
    assert _stats(call, doc, "L")["pixel_count"] == 38
    trimmed = call("trim_layers", doc=doc, ids=["L"], alpha_threshold=8, near_px=2)
    assert trimmed["trimmed"][0]["cleared"] == 1
    assert trimmed["trimmed"][0]["bounds_after"] == {"x": 4, "y": 4, "w": 7, "h": 6}
    assert _stats(call, doc, "L")["pixel_count"] == 37
    call.error("conflict", "trim_layers", doc=doc, ids=["G"], alpha_threshold=8)

    call("undo", doc=doc)
    assert _stats(call, doc, "L")["pixel_count"] == 38
