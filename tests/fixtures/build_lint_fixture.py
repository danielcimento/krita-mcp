"""Build ``tests/fixtures/lint_four_findings.kra``, acceptance test 4's
document (acceptance.md § Fixtures, Lint fixture). Runs inside Krita.

Headless, through the shim (filter layers can be made headless,
krita-api-notes § Nodes, so no Scripter is needed)::

    make lint-fixture
    # = kritarunner -s kmcp_test_runner -f build_lint_fixture <repo> <out.kra>

``tests/krita/test_lint_fixture.py`` also calls ``build`` into a temp dir
and lints the result, so the builder stays correct; the tests otherwise
read the committed file and never rebuild it.

64x64, RGBA / U8, the default sRGB profile. Stack, top to bottom::

    Blur      filter layer (Gaussian blur, whole canvas)  -> node-type
    Head      group
      Hair    paint, opaque 8x8 at (20, 4)
    Back      group
      Hair    paint, opaque 8x8 at (36, 4)                -> duplicate-name
    Shade     paint, inherit alpha, opaque 16x16 at (16, 24),
              clipped onto Body                           -> inherit-alpha
    Body      paint, opaque 32x32 at (16, 24)
    Spare     paint, no pixels                            -> empty-layer

Nothing else is set, so every other default rule stays quiet. A rebuild
differs from the committed file only in layer UUIDs and timestamps.

Runs on Krita's Python (3.13); needs ``krita-plugin`` on ``sys.path``
(for ``kritamcp.fileio``, the batch-mode save).
"""

import os
import shutil
import tempfile

from krita import Krita, Selection

WIDTH = HEIGHT = 64
NAME = "lint_four_findings"

# name -> (x, y, w, h, (b, g, r)) of an opaque rectangle.
RECTS = {
    "Body": (16, 24, 32, 32, (60, 110, 200)),
    "Shade": (16, 24, 16, 16, (40, 70, 130)),
    "Back Hair": (36, 4, 8, 8, (30, 40, 90)),
    "Head Hair": (20, 4, 8, 8, (30, 40, 90)),
}


def _paint(doc, parent, name, rect=None):
    """A paint layer named ``name`` on top of ``parent``'s children, filled
    with ``RECTS[rect]`` (opaque) if given."""
    node = doc.createNode(name, "paintlayer")
    parent.addChildNode(node, None)
    if rect is not None:
        x, y, w, h, (b, g, r) = RECTS[rect]
        if not node.setPixelData(bytes((b, g, r, 255)) * (w * h), x, y, w, h):
            raise RuntimeError(f"setPixelData failed on {name}")
    return node


def _group(doc, parent, name):
    group = doc.createGroupLayer(name)
    parent.addChildNode(group, None)
    return group


def _populate(doc):
    app = Krita.instance()
    root = doc.rootNode()
    for child in root.childNodes():  # the default Background layer
        if not child.remove():
            raise RuntimeError(f"could not remove {child.name()}")
    # Bottom to top: each addChildNode(node, None) puts node on top.
    _paint(doc, root, "Spare")
    _paint(doc, root, "Body", "Body")
    shade = _paint(doc, root, "Shade", "Shade")
    shade.setInheritAlpha(True)
    back = _group(doc, root, "Back")
    _paint(doc, back, "Hair", "Back Hair")
    head = _group(doc, root, "Head")
    _paint(doc, head, "Hair", "Head Hair")
    sel = Selection()
    sel.select(0, 0, WIDTH, HEIGHT, 255)
    blur = doc.createFilterLayer("Blur", app.filter("gaussian blur"), sel)
    if blur is None:
        raise RuntimeError("createFilterLayer returned None")
    root.addChildNode(blur, None)
    doc.refreshProjection()
    doc.waitForDone()


def build(path):
    """Build the fixture and save it as ``path`` (a ``.kra``; replaced if it
    exists). Saved into a temp dir first, then moved, so Krita never writes
    a backup file or a half-written file at ``path``. Returns the size in
    bytes."""
    from kritamcp import fileio

    path = os.path.abspath(path)
    tmp = tempfile.mkdtemp(prefix="kmcp-fixture-")
    try:
        staged = os.path.join(tmp, os.path.basename(path))
        doc = Krita.instance().createDocument(WIDTH, HEIGHT, NAME, "RGBA", "U8", "", 72.0)
        if doc is None:
            raise RuntimeError("createDocument returned None")
        try:
            _populate(doc)
            fileio.save_as(doc, staged)
        finally:
            doc.setModified(False)  # so close() never prompts
            doc.close()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.move(staged, path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return os.path.getsize(path)
