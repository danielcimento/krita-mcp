"""
krita-mcp F001 API probe.

Answers the open questions in docs/f001_buildout/spike.md by running small
experiments against Krita's Python API.

How to run:
  1. Krita: Tools > Scripts > Scripter
  2. File > Open this file, then Run (or Ctrl+R)
  3. Results print in Scripter's output pane and are written to
     ~/krita-mcp-output/spike/probe-results.json

It only touches documents it creates itself (named "probe-*"), and closes
them at the end without saving. Several probes open a view so that
Edit > Undo has a target, so tabs will flash. Set RUN_LARGE = True for the
Mao-sized timing probe (Q11): it needs about 2 GB of RAM and a minute or two.

If a probe hangs on a dialog (a save or close prompt), that is itself the
answer for Q13: dismiss it and record that in spike.md.
"""

import json
import math
import os
import sys
import time
import traceback

from krita import Krita, InfoObject, Selection
from PyQt5.QtCore import QPointF, QRect, QUuid
from PyQt5.QtGui import QImage

OUT_DIR = os.path.expanduser("~/krita-mcp-output/spike")
RUN_LARGE = False
MAO_ORA = os.path.expanduser("~/krita-mcp-output/mao_pro_layered.ora")

app = Krita.instance()
results = {}
PROBES = []
SCRATCH = []


# ---------------------------------------------------------------- helpers

def probe(qid, title):
    def deco(fn):
        def run():
            t0 = time.perf_counter()
            try:
                out, status = fn(), "ok"
            except Exception as e:  # record and keep going
                out = {"exception": repr(e), "traceback": traceback.format_exc()}
                status = "error"
            results[qid] = {
                "title": title,
                "status": status,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                "result": out,
            }
            print(f"[{qid}] {title}: {status}\n    {json.dumps(out, default=str)[:600]}")
        PROBES.append(run)
        return fn
    return deco


def scratch_doc(w=256, h=256, name="probe"):
    doc = app.createDocument(w, h, f"probe-{name}", "RGBA", "U8", "", 72.0)
    SCRATCH.append(doc)
    return doc


def show(doc):
    """Open a view on doc and make it active, so edit_undo targets it."""
    win = app.activeWindow()
    view = win.addView(doc)
    win.showView(view)
    app.setActiveDocument(doc)
    return view


def settle(doc):
    doc.refreshProjection()
    doc.waitForDone()


def solid(b, g, r, a, n):
    return bytes((b, g, r, a)) * n


def add_paint(doc, name="p", parent=None):
    node = doc.createNode(name, "paintlayer")
    (parent or doc.rootNode()).addChildNode(node, None)
    return node


def rect(r):
    return [r.x(), r.y(), r.width(), r.height()]


def px(node, x, y):
    """BGRA bytes of one pixel as a list of ints."""
    return list(bytes(node.pixelData(x, y, 1, 1)))


def names(node):
    return [c.name() for c in node.childNodes()]


def walk(node):
    """All descendants of node, depth-first."""
    for c in node.childNodes():
        yield c
        yield from walk(c)


def ids_by_name(doc):
    return {n.name(): n.uniqueId().toString() for n in walk(doc.rootNode())}


# ----------------------------------------------------------------- probes

@probe("Q0", "Environment")
def q0():
    try:
        import numpy  # noqa: F401
        has_numpy = True
    except ImportError:
        has_numpy = False
    return {
        "krita": app.version(),
        "python": sys.version,
        "numpy": has_numpy,
        "byteorder": sys.byteorder,
        "filter_strategies": list(app.filterStrategies()),
    }


@probe("Q1", "Which libkis calls create undo steps")
def q1():
    """Each call runs on a fresh document so a non-undoable call can't
    make edit_undo revert an earlier probe's change. 'undoable' means one
    Edit > Undo restored exactly the state before the call."""
    undo = app.action("edit_undo")
    out = {}

    def check(label, setup, do, state):
        doc = scratch_doc(name=f"undo-{label}")
        node = setup(doc)
        settle(doc)
        show(doc)
        before = state(doc, node)
        do(doc, node)
        settle(doc)
        after = state(doc, node)
        undo.trigger()
        settle(doc)
        undone = state(doc, node)
        out[label] = {
            "active_is_doc": app.activeDocument() == doc,
            "before": before, "after": after, "after_undo": undone,
            "changed": after != before,
            "undoable": after != before and undone == before,
        }

    def with_layer(doc):
        n = add_paint(doc, "A")
        n.setPixelData(solid(0, 0, 255, 255, 16), 0, 0, 4, 4)
        return n

    def state(prop):
        return lambda doc, n: {"prop": prop(n), "tree": names(doc.rootNode())}

    check("addChildNode",
          lambda d: d.createNode("A", "paintlayer"),
          lambda d, n: d.rootNode().addChildNode(n, None),
          lambda d, n: {"tree": names(d.rootNode())})
    check("setName", with_layer, lambda d, n: n.setName("B"), state(lambda n: n.name()))
    check("setVisible", with_layer, lambda d, n: n.setVisible(False), state(lambda n: n.visible()))
    check("setOpacity", with_layer, lambda d, n: n.setOpacity(100), state(lambda n: n.opacity()))
    check("setBlendingMode", with_layer, lambda d, n: n.setBlendingMode("multiply"),
          state(lambda n: n.blendingMode()))
    check("setInheritAlpha", with_layer, lambda d, n: n.setInheritAlpha(True),
          state(lambda n: n.inheritAlpha()))
    check("setPixelData", with_layer,
          lambda d, n: n.setPixelData(solid(255, 0, 0, 255, 16), 0, 0, 4, 4),
          state(lambda n: px(n, 1, 1)))
    check("move", with_layer, lambda d, n: n.move(20, 30),
          state(lambda n: rect(n.bounds())))
    check("remove", with_layer, lambda d, n: n.remove(), lambda d, n: {"tree": names(d.rootNode())})
    def reorder(d, n):
        """Move 'Bottom' above 'Top'."""
        root = d.rootNode()
        root.removeChildNode(n)
        root.addChildNode(n, root.childNodes()[-1])

    check("removeChildNode+addChildNode (reorder)",
          lambda d: (add_paint(d, "Bottom"), add_paint(d, "Top"))[0],
          reorder,
          lambda d, n: {"tree": names(d.rootNode())})
    check("setActiveNode", with_layer,
          lambda d, n: d.setActiveNode(d.rootNode().childNodes()[0]),
          lambda d, n: {"active": d.activeNode().name() if d.activeNode() else None,
                        "tree": names(d.rootNode())})
    return out


@probe("Q2", "Node.bounds(): exact or tile-aligned; empty layers")
def q2():
    doc = scratch_doc(name="bounds")
    n = add_paint(doc)
    empty_bounds, empty_ext = rect(n.bounds()), n.hasExtents()
    n.setPixelData(solid(0, 0, 255, 255, 100), 13, 17, 10, 10)
    settle(doc)
    after_write = rect(n.bounds())
    n.setPixelData(bytes(4 * 100), 13, 17, 10, 10)  # clear back to transparent
    settle(doc)
    after_clear = rect(n.bounds())
    off = add_paint(doc, "off-canvas")
    off.setPixelData(solid(0, 0, 255, 255, 100), 250, 250, 10, 10)  # crosses the edge
    settle(doc)
    return {
        "empty_bounds": empty_bounds, "empty_hasExtents": empty_ext,
        "after_write": after_write, "expected_exact": [13, 17, 10, 10],
        "exact": after_write == [13, 17, 10, 10],
        "after_clear_to_transparent": after_clear,
        "off_canvas_bounds": rect(off.bounds()),
        "off_canvas_pixel_outside_readable": px(off, 258, 258),
    }


@probe("Q3", "UUIDs: format, lookup, clone, .kra/.ora persistence")
def q3():
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = scratch_doc(name="uuid")
    g = doc.createGroupLayer("Group")
    doc.rootNode().addChildNode(g, None)
    a = add_paint(doc, "Alpha", parent=g)
    a.setPixelData(solid(0, 0, 255, 255, 4), 0, 0, 2, 2)
    settle(doc)
    ids = {n.name(): n.uniqueId().toString() for n in (g, a)}
    found = doc.nodeByUniqueID(QUuid(ids["Alpha"]))
    found_unbraced = doc.nodeByUniqueID(QUuid(ids["Alpha"].strip("{}")))

    clone = doc.clone()
    SCRATCH.append(clone)
    clone_ids = ids_by_name(clone)

    persisted = {}
    for ext in ("kra", "ora"):
        path = os.path.join(OUT_DIR, f"probe-uuid.{ext}")
        doc.setBatchmode(True)
        ok = doc.exportImage(path, InfoObject())
        doc.setBatchmode(False)
        re = app.openDocument(path)
        if re is None:
            persisted[ext] = {"export_ok": ok, "reopen": None}
            continue
        SCRATCH.append(re)
        re_ids = ids_by_name(re)
        persisted[ext] = {"export_ok": ok, "same_ids": re_ids == ids, "ids": re_ids,
                          "reopened_name": re.name(), "reopened_fileName": re.fileName()}
    return {
        "ids": ids,
        "lookup_ok": found is not None and found == a,
        "lookup_unbraced_ok": found_unbraced is not None and found_unbraced == a,
        "node_wrappers_equal": doc.rootNode().childNodes()[0] == g,
        "clone_keeps_ids": clone_ids == ids, "clone_ids": clone_ids,
        "persisted": persisted,
    }


@probe("Q4", "createDocument defaults, profiles, Document equality")
def q4():
    doc = scratch_doc(name="defaults")
    profiles = list(app.profiles("RGBA", "U8"))
    return {
        "default_children": [(c.name(), c.type()) for c in doc.rootNode().childNodes()],
        "color": [doc.colorModel(), doc.colorDepth(), doc.colorProfile()],
        "name": doc.name(), "fileName": doc.fileName(), "modified": doc.modified(),
        "profiles_rgba_u8": profiles[:40], "profiles_count": len(profiles),
        "in_documents_by_eq": any(d == doc for d in app.documents()),
        "in_documents_by_is": any(d is doc for d in app.documents()),
    }


@probe("Q5", "BGRA order and bit-exact PNG round trip via QImage")
def q5():
    os.makedirs(OUT_DIR, exist_ok=True)
    w, h = 256, 4
    doc = scratch_doc(w, h, name="bgra")
    n = add_paint(doc)
    # Every alpha value, with distinct colors per row.
    data = bytearray()
    for y in range(h):
        for x in range(w):
            data += bytes(((x * 7 + y) % 256, (x * 13) % 256, (255 - x + y * 50) % 256, x))
    n.setPixelData(bytes(data), 0, 0, w, h)
    settle(doc)
    back = bytes(n.pixelData(0, 0, w, h))

    opaque = add_paint(doc, "opaque")
    opaque.setPixelData(bytes((10, 20, 30, 255)), 5, 0, 1, 1)
    settle(doc)
    c = doc.projection(5, 0, 1, 1).pixelColor(0, 0)

    img = QImage(back, w, h, 4 * w, QImage.Format_ARGB32).copy()
    path = os.path.join(OUT_DIR, "probe-bgra.png")
    saved = img.save(path, "PNG")
    re = QImage(path).convertToFormat(QImage.Format_ARGB32)
    re_bytes = re.constBits().asstring(re.sizeInBytes())

    def strip_zero_alpha(buf):
        b = bytearray(buf)
        for i in range(0, len(b), 4):
            if b[i + 3] == 0:
                b[i:i + 3] = b"\0\0\0"
        return bytes(b)

    return {
        "setPixelData_roundtrip_exact": back == bytes(data),
        "setPixelData_roundtrip_exact_ignoring_zero_alpha":
            strip_zero_alpha(back) == strip_zero_alpha(bytes(data)),
        "projection_of_bgra_10_20_30": [c.red(), c.green(), c.blue(), c.alpha()],
        "expected_rgb": [30, 20, 10, 255],
        "qimage_png_saved": saved,
        "png_roundtrip_exact": re_bytes == back,
        "png_roundtrip_exact_ignoring_zero_alpha":
            strip_zero_alpha(re_bytes) == strip_zero_alpha(back),
        "bytesPerLine": re.bytesPerLine(),
    }


@probe("Q6", "move / scaleNode / rotateNode semantics")
def q6():
    doc = scratch_doc(name="xform")
    out = {}

    def fresh(name):
        n = add_paint(doc, name)
        n.setPixelData(solid(0, 0, 255, 255, 200), 100, 50, 20, 10)
        settle(doc)
        return n

    n = fresh("move")
    p0, b0 = n.position(), rect(n.bounds())
    n.move(10, 10)
    settle(doc)
    out["move(10,10)"] = {"pos_before": [p0.x(), p0.y()], "bounds_before": b0,
                          "pos_after": [n.position().x(), n.position().y()],
                          "bounds_after": rect(n.bounds())}
    n.move(10, 10)
    settle(doc)
    out["move(10,10) again"] = {"bounds_after": rect(n.bounds()),
                                "note": "same bounds => absolute, shifted => relative"}

    n = fresh("scale")
    n.scaleNode(QPointF(100, 50), 40, 20, "Bicubic")
    settle(doc)
    out["scaleNode(origin=100,50, 40x20)"] = {"bounds_before": [100, 50, 20, 10],
                                              "bounds_after": rect(n.bounds())}

    n = fresh("rotate")
    n.rotateNode(math.pi / 2)
    settle(doc)
    out["rotateNode(pi/2)"] = {"bounds_before": [100, 50, 20, 10],
                               "bounds_after": rect(n.bounds()),
                               "image_center": [doc.width() / 2, doc.height() / 2]}
    return out


@probe("Q7", "Visibility toggles: modified flag and projection refresh")
def q7():
    doc = scratch_doc(name="vis")
    n = add_paint(doc)
    n.setPixelData(solid(0, 0, 255, 255, 100), 10, 10, 10, 10)
    settle(doc)
    doc.setModified(False)
    before = doc.projection(15, 15, 1, 1).pixelColor(0, 0).alpha()
    n.setVisible(False)
    settle(doc)
    hidden = doc.projection(15, 15, 1, 1).pixelColor(0, 0).alpha()
    modified_after_hide = doc.modified()
    n.setVisible(True)
    settle(doc)
    doc.setModified(False)
    t0 = time.perf_counter()
    c = doc.clone()
    clone_ms = round((time.perf_counter() - t0) * 1000)
    SCRATCH.append(c)
    return {
        "alpha_visible": before, "alpha_hidden": hidden,
        "projection_follows_visibility": before == 255 and hidden == 0,
        "modified_after_setVisible": modified_after_hide,
        "modified_after_restore_and_reset": doc.modified(),
        "clone_ms_small_doc": clone_ms,
    }


@probe("Q8", "Group projection: own opacity included? clipping baked?")
def q8():
    doc = scratch_doc(name="group")
    g = doc.createGroupLayer("G")
    doc.rootNode().addChildNode(g, None)
    base = add_paint(doc, "Base", parent=g)
    base.setPixelData(solid(0, 0, 255, 255, 100), 0, 0, 10, 10)
    clip = doc.createNode("Clip", "paintlayer")
    g.addChildNode(clip, base)
    clip.setPixelData(solid(255, 0, 0, 255, 400), 0, 0, 20, 20)
    clip.setInheritAlpha(True)
    g.setOpacity(128)
    settle(doc)
    return {
        "group_projection_alpha_at_5_5": list(bytes(g.projectionPixelData(5, 5, 1, 1))),
        "group_projection_alpha_at_15_15": list(bytes(g.projectionPixelData(15, 15, 1, 1))),
        "note": "alpha 255 at (5,5) => group opacity NOT in its projection; "
                "alpha 0 at (15,15) => clipping is baked",
        "doc_projection_at_5_5": doc.projection(5, 5, 1, 1).pixelColor(0, 0).getRgb(),
    }


@probe("Q9", "Selection copy/cut/paste and document selection")
def q9():
    doc = scratch_doc(name="sel")
    src = add_paint(doc, "Src")
    src.setPixelData(solid(0, 0, 255, 255, 400), 0, 0, 20, 20)
    dst = add_paint(doc, "Dst")
    settle(doc)
    sel = Selection()
    sel.select(0, 0, 5, 5, 255)
    doc.setSelection(sel)
    got = doc.selection()
    out = {"doc_selection_rect": [got.x(), got.y(), got.width(), got.height()] if got else None}
    sel.cut(src)
    sel.paste(dst, 0, 0)
    settle(doc)
    out.update({
        "src_at_2_2_after_cut": px(src, 2, 2), "src_at_10_10": px(src, 10, 10),
        "dst_at_2_2_after_paste": px(dst, 2, 2),
        "dst_children_after_paste": names(doc.rootNode()),
        "note": "paste may create a floating/new layer instead of writing into dst",
    })
    doc.setSelection(None)
    out["selection_cleared"] = doc.selection() is None
    return out


@probe("Q10", "Selection.grow as dilation")
def q10():
    sel = Selection()
    sel.resize(64, 64)  # may be needed before setPixelData
    mask = bytearray(64 * 64)
    mask[32 * 64 + 32] = 255
    sel.setPixelData(bytes(mask), 0, 0, 64, 64)
    sel.grow(4, 4)
    data = bytes(sel.pixelData(0, 0, 64, 64))
    nz = sum(1 for v in data if v)
    xs = [i % 64 for i, v in enumerate(data) if v]
    ys = [i // 64 for i, v in enumerate(data) if v]
    return {
        "nonzero_after_grow_4": nz,
        "bbox": [min(xs), min(ys), max(xs), max(ys)] if xs else None,
        "note": "81 => square kernel, about 49-57 => disc; bbox should be 28..36",
    }


@probe("Q11", "Timing at Mao size (RUN_LARGE only)")
def q11():
    if not RUN_LARGE:
        return {"skipped": "set RUN_LARGE = True"}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = {}
    w, h = 5800, 8400

    def t(label, fn):
        t0 = time.perf_counter()
        r = fn()
        out[label + "_ms"] = round((time.perf_counter() - t0) * 1000)
        return r

    doc = t("createDocument", lambda: scratch_doc(w, h, name="large"))
    n = add_paint(doc)
    buf = t("build_buffer", lambda: solid(40, 80, 120, 255, w * h))
    t("setPixelData_full", lambda: n.setPixelData(buf, 0, 0, w, h))
    t("settle", lambda: settle(doc))
    data = t("pixelData_full", lambda: bytes(n.pixelData(0, 0, w, h)))
    t("alpha_slice", lambda: data[3::4])
    img = QImage(data, w, h, 4 * w, QImage.Format_ARGB32)
    t("qimage_save_png", lambda: img.save(os.path.join(OUT_DIR, "probe-large.png"), "PNG"))
    t("projection_full", lambda: doc.projection(0, 0, w, h))
    doc.setBatchmode(True)
    t("exportImage_png", lambda: doc.exportImage(os.path.join(OUT_DIR, "probe-large-export.png"),
                                                 InfoObject()))
    t("saveAs_kra", lambda: doc.saveAs(os.path.join(OUT_DIR, "probe-large.kra")))
    doc.setBatchmode(False)
    t("clone", lambda: SCRATCH.append(doc.clone()))
    if os.path.exists(MAO_ORA):
        mao = t("open_mao_ora", lambda: app.openDocument(MAO_ORA))
        if mao:
            SCRATCH.append(mao)
            t("mao_settle", lambda: settle(mao))
            t("mao_projection", lambda: mao.projection(0, 0, mao.width(), mao.height()))
            t("mao_clone", lambda: SCRATCH.append(mao.clone()))
            out["mao_name"] = mao.name()
            out["mao_nodes"] = len(list(walk(mao.rootNode())))
    return out


@probe("Q13", "Batch-mode save/export/close: return values, fileName, dialogs")
def q13():
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = scratch_doc(name="save")
    add_paint(doc).setPixelData(solid(0, 0, 255, 255, 4), 0, 0, 2, 2)
    settle(doc)
    doc.setBatchmode(True)
    png = os.path.join(OUT_DIR, "probe-save.png")
    kra = os.path.join(OUT_DIR, "probe-save.kra")
    out = {"export_png_ok": doc.exportImage(png, InfoObject()),
           "fileName_after_export": doc.fileName()}
    out["saveAs_kra_ok"] = doc.saveAs(kra)
    out["fileName_after_saveAs"] = doc.fileName()
    out["modified_after_saveAs"] = doc.modified()
    doc.setBatchmode(False)
    victim = scratch_doc(32, 32, name="close")
    add_paint(victim)
    victim.setModified(True)
    victim.setModified(False)
    out["close_after_setModified_false"] = victim.close()
    return out


@probe("Q14", "layerStyleToAsl on a plain layer; Node.save with exportRect")
def q14():
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = scratch_doc(name="nodesave")
    n = add_paint(doc)
    n.setPixelData(solid(0, 0, 255, 200, 100), 13, 17, 10, 10)
    n.setOpacity(128)
    settle(doc)
    asl = n.layerStyleToAsl()
    path = os.path.join(OUT_DIR, "probe-nodesave.png")
    n.save(path, 72.0, 72.0, InfoObject(), QRect(13, 17, 10, 10))
    img = QImage(path)
    c = img.pixelColor(0, 0) if not img.isNull() else None
    return {
        "asl_repr": repr(asl)[:200], "asl_empty": not asl,
        "node_save_size": [img.width(), img.height()],
        "node_save_pixel_rgba": [c.red(), c.green(), c.blue(), c.alpha()] if c else None,
        "note": "alpha 200 => raw pixels; ~100 => layer opacity baked in",
    }


@probe("Q15", "Blend mode IDs and invalid values")
def q15():
    doc = scratch_doc(name="blend")
    n = add_paint(doc)
    out = {}
    for mode in ("normal", "multiply", "add", "screen", "linear_dodge", "not-a-mode"):
        n.setBlendingMode(mode)
        out[mode] = n.blendingMode()
    return out


# ------------------------------------------------------------------ main

def main():
    try:
        for run in PROBES:
            run()
    finally:
        for d in SCRATCH:
            try:
                d.setModified(False)
                d.close()
            except Exception:
                pass
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "probe-results.json")
        with open(path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nWrote {path}")


main()
