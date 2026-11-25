"""Throwaway probe: Krita `add` vs `linear_dodge` (pixels, PSD/ORA export).
kritarunner -s kmcp_blend_probe -f run <outdir>
"""
import json, os, re, struct, sys, traceback, zipfile

from krita import Krita


def _close(doc):
    try:
        doc.setModified(False)
    finally:
        doc.close()


def _grid_doc(app, depth, mode, src_alpha=255, layer_opacity=255):
    """256x256: top layer value = x, base value = y (gray). Returns projection bytes."""
    doc = app.createDocument(256, 256, "probe", "RGBA", depth, "", 72.0)
    try:
        root = doc.rootNode()
        for c in list(root.childNodes()):
            c.remove()
        base = doc.createNode("base", "paintlayer")
        root.addChildNode(base, None)
        top = doc.createNode("top", "paintlayer")
        root.addChildNode(top, base)

        def px(v, a):
            if depth == "U8":
                return bytes((v, v, v, a))
            if depth == "U16":
                return struct.pack("<4H", v * 257, v * 257, v * 257, a * 257)
            return struct.pack("<4f", v / 255, v / 255, v / 255, a / 255)

        base_rows = b"".join(px(y, 255) * 256 for y in range(256))
        top_row = b"".join(px(x, src_alpha) for x in range(256))
        base.setPixelData(base_rows, 0, 0, 256, 256)
        top.setPixelData(top_row * 256, 0, 0, 256, 256)
        top.setBlendingMode(mode)
        top.setOpacity(layer_opacity)
        doc.refreshProjection()
        doc.waitForDone()
        return bytes(doc.pixelData(0, 0, 256, 256))
    finally:
        _close(doc)


def pixels(app):
    out = {}
    for depth in ("U8", "U16", "F32"):
        for label, sa, op in (("opaque", 255, 255), ("src_alpha_128", 128, 255), ("opacity_128", 255, 128)):
            a = _grid_doc(app, depth, "add", sa, op)
            b = _grid_doc(app, depth, "linear_dodge", sa, op)
            n = _grid_doc(app, depth, "normal", sa, op)
            r = {"identical": a == b, "differs_from_normal": a != n, "bytes": len(a)}
            if a != b:
                step = len(a) // (256 * 256)
                diffs = [i for i in range(0, len(a), step) if a[i:i + step] != b[i:i + step]]
                r["differing_pixels"] = len(diffs)
                i = diffs[0]
                r["first_diff"] = {"pixel": [(i // step) % 256, (i // step) // 256],
                                   "add": list(a[i:i + step]), "linear_dodge": list(b[i:i + step])}
            if depth == "U8" and label == "opaque":
                exp = b"".join(bytes((min(255, x + y),) * 3 + (255,)) for y in range(256) for x in range(256))
                r["add_equals_clamped_sum"] = a == exp
                r["linear_dodge_equals_clamped_sum"] = b == exp
            out[f"{depth}/{label}"] = r
    return out


def _psd_keys(path):
    data = open(path, "rb").read()
    blend_keys = {b"norm", b"mul ", b"lddg", b"scrn", b"dark", b"lite", b"over", b"diss", b"pass",
                  b"lbrn", b"idiv", b"div ", b"diff", b"smud", b"fsub", b"fdiv", b"colr", b"lum ",
                  b"hue ", b"sat ", b"hLit", b"sLit", b"vLit", b"lLit", b"pLit", b"hMix", b"dkCl", b"lgCl"}
    return [m.group(1).decode("latin-1") for m in re.finditer(rb"8BIM(.{4})", data, re.S)
            if m.group(1) in blend_keys]


def exports(app, outdir):
    res = {}
    doc = app.createDocument(64, 64, "probe-export", "RGBA", "U8", "", 72.0)
    try:
        root = doc.rootNode()
        for c in list(root.childNodes()):
            c.remove()
        prev = None
        for name, mode in (("Base Normal", "normal"), ("Shade Multiply", "multiply"),
                           ("Glow Add", "add"), ("Glow LinearDodge", "linear_dodge")):
            n = doc.createNode(name, "paintlayer")
            root.addChildNode(n, prev)
            n.setPixelData(bytes((90, 90, 90, 255)) * (16 * 16), 8, 8, 16, 16)
            n.setBlendingMode(mode)
            prev = n
        doc.refreshProjection()
        doc.waitForDone()
        doc.setBatchmode(True)
        for ext in ("psd", "ora", "kra"):
            path = os.path.join(outdir, f"probe-blend.{ext}")
            if os.path.exists(path):
                os.remove(path)
            ok = doc.exportImage(path, __import__("krita").InfoObject())
            res[ext] = {"export_ok": ok, "written": os.path.isfile(path)}
        doc.setBatchmode(False)
    finally:
        _close(doc)

    psd = os.path.join(outdir, "probe-blend.psd")
    if os.path.isfile(psd):
        res["psd"]["raw_blend_keys_in_file_order"] = _psd_keys(psd)
    ora = os.path.join(outdir, "probe-blend.ora")
    if os.path.isfile(ora):
        with zipfile.ZipFile(ora) as z:
            xml = z.read("stack.xml").decode("utf-8")
        res["ora"]["stack_xml_layers"] = re.findall(r'<layer[^>]*>', xml)

    for ext in ("psd", "ora", "kra"):
        path = os.path.join(outdir, f"probe-blend.{ext}")
        if not os.path.isfile(path):
            continue
        app.setBatchmode(True)
        try:
            d = app.openDocument(path)
        finally:
            app.setBatchmode(False)
        if d is None:
            res[ext]["reopen"] = None
            continue
        try:
            res[ext]["reopen"] = {c.name(): c.blendingMode() for c in d.rootNode().childNodes()}
        finally:
            _close(d)
    return res


def run(args=None):
    outdir = args[0] if args else os.path.expanduser("~/krita-mcp-output/spike")
    os.makedirs(outdir, exist_ok=True)
    app = Krita.instance()
    r = {"krita": app.version()}
    for key, fn in (("pixels", lambda: pixels(app)), ("exports", lambda: exports(app, outdir))):
        try:
            r[key] = fn()
        except Exception:
            r[key] = {"error": traceback.format_exc()}
    with open(os.path.join(outdir, "blend-probe.json"), "w") as f:
        json.dump(r, f, indent=2)
    print("KMCP_BLEND_PROBE_DONE")
