"""Throwaway probe: Krita pass-through vs isolated group rendering."""
import json, os
from krita import Krita


def _px(doc):
    doc.refreshProjection(); doc.waitForDone()
    b, g, r, a = bytes(doc.pixelData(2, 2, 1, 1))
    return [r, g, b, a]


def case(app, pass_through, group_opacity, child_mode, group_mode="normal", base=(0, 0, 255, 255)):
    doc = app.createDocument(8, 8, "pt", "RGBA", "U8", "", 72.0)
    try:
        root = doc.rootNode()
        for c in list(root.childNodes()):
            c.remove()
        bottom = doc.createNode("Base", "paintlayer")
        root.addChildNode(bottom, None)
        bottom.setPixelData(bytes(base) * 64, 0, 0, 8, 8)  # BGRA: red
        grp = doc.createGroupLayer("G")
        root.addChildNode(grp, bottom)
        child = doc.createNode("C", "paintlayer")
        grp.addChildNode(child, None)
        child.setPixelData(bytes((128, 128, 128, 255)) * 64, 0, 0, 8, 8)  # gray
        child.setBlendingMode(child_mode)
        grp.setPassThroughMode(pass_through)
        grp.setOpacity(round(group_opacity * 255))
        grp.setBlendingMode(group_mode)
        return _px(doc)
    finally:
        doc.setModified(False); doc.close()


def run(args=None):
    app = Krita.instance()
    out = {}
    for pt in (True, False):
        for op in (1.0, 0.5):
            for mode in ("normal", "multiply", "add"):
                out[f"pass_through={pt} opacity={op} child={mode}"] = case(app, pt, op, mode)
    out["isolated group blend multiply, child normal"] = case(app, False, 1.0, "normal", "multiply")
    out["pass_through group blend multiply, child normal"] = case(app, True, 1.0, "normal", "multiply")
    path = args[0] if args else os.path.expanduser("~/krita-mcp-output/pt-probe.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print("KMCP_PT_PROBE_DONE")
