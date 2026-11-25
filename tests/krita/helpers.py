"""Helpers for the headless suite (runs inside Krita via kritarunner).

Calling any method on a closed Document crashes Krita, so documents are
only ever closed here, in ``finally``, and never touched afterwards.
"""

import contextlib

from krita import Krita


@contextlib.contextmanager
def scratch_document(width=16, height=16, name="kmcp-test", color_model="RGBA",
                     color_depth="U8", profile="", resolution=72.0):
    """Create a throwaway document and always close it.

    The test must not close the document itself or keep a reference to it
    (or its nodes) after the ``with`` block.
    """
    doc = Krita.instance().createDocument(
        width, height, name, color_model, color_depth, profile, resolution)
    if doc is None:
        raise RuntimeError("createDocument returned None")
    try:
        yield doc
    finally:
        try:
            doc.setModified(False)  # so close() never prompts
        finally:
            doc.close()


def add_paint_layer(doc, name="layer"):
    """Create a paint layer at the top of the root and return it."""
    node = doc.createNode(name, "paintlayer")
    doc.rootNode().addChildNode(node, None)
    return node


def settle(doc):
    """Wait for Krita to finish pending work on ``doc``."""
    doc.refreshProjection()
    doc.waitForDone()
