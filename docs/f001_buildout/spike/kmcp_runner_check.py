"""Q12 check: run under kritarunner (headless). Install by copying into
~/Library/Application Support/kritarunner/pykrita/ (kritarunner has its own
profile, separate from the GUI Krita), then:
  /Applications/krita.app/Contents/MacOS/kritarunner -s kmcp_runner_check -f run hello
kritarunner calls run() with the argument list. PYTHONPATH is ignored.
"""
import json, os, sys
OUT = os.path.expanduser("~/krita-mcp-output/spike")

def run(args=None):
    from krita import Krita
    app = Krita.instance()
    r = {"args": repr(args), "version": app.version(), "python": sys.version.split()[0],
         "windows": len(app.windows()), "sys_path_head": sys.path[:3]}
    d = app.createDocument(16, 16, "runner", "RGBA", "U8", "", 72.0)
    n = d.createNode("p", "paintlayer"); d.rootNode().addChildNode(n, None)
    n.setPixelData(bytes((1, 2, 3, 255)) * 4, 2, 2, 2, 2)
    d.refreshProjection(); d.waitForDone()
    d.setBatchmode(True)
    kra = os.path.join(OUT, "runner.kra")
    r["saveAs"] = d.saveAs(kra)
    r["bounds"] = [n.bounds().x(), n.bounds().y(), n.bounds().width(), n.bounds().height()]
    d.close()
    re = app.openDocument(kra)
    r["reopen_layers"] = [c.name() for c in re.rootNode().childNodes()] if re else None
    r["reopen_pixel"] = list(bytes(re.rootNode().childNodes()[-1].pixelData(2, 2, 1, 1))) if re else None
    if re: re.close()
    with open(os.path.join(OUT, "runner-result.json"), "w") as f:
        json.dump(r, f, indent=2)
    print("KMCP_RESULT", json.dumps(r))
