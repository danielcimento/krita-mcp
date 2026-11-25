"""Acceptance test 1 (Mao rebuild), headless (P2.4).

Skipped unless ``KRITA_MCP_MAO_ORA`` names the Mao ``.ora``
(acceptance.md § Fixtures). kritarunner is started by ``make test-krita``
from the shell and inherits its environment, so::

    KRITA_MCP_MAO_ORA=~/krita-mcp-output/mao_pro_layered.ora make test-krita KRITA_TESTS=mao

The Mao art is third-party: everything derived from it (PNGs, manifest,
``.kra``) goes into one temp dir, made the only extra allowed root, and
deleted in ``finally``. Timings are printed as ``KMCP_MAO: ...`` lines to
kritarunner's stdout, i.e. ``~/krita-mcp-output/test-krita/run.log``.
"""

import collections
import os
import shutil
import sys
import tempfile
import time
import unittest

from PyQt5.QtGui import QImage

from helpers import temp_config
from kritamcp import registry
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run

MAO_ENV = "KRITA_MCP_MAO_ORA"
TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools")
WIDTH, HEIGHT = 5800, 8400
HIDDEN_GROUPS = ["Expression References", "[ Guide Image ]"]


def opaque_bounds(path):
    """``(x, y, w, h)`` of the pixels with alpha > 0 in the PNG at ``path``,
    or None if it has none. Row scans over ``bytes`` (C speed), no
    per-pixel Python loop."""
    image = QImage(path)
    if image.isNull():
        raise RuntimeError(f"cannot decode {path}")
    alpha = image.convertToFormat(QImage.Format_Alpha8)
    w, h, bpl = alpha.width(), alpha.height(), alpha.bytesPerLine()
    ptr = alpha.constBits()
    ptr.setsize(bpl * h)
    buf = bytes(ptr)
    top = bottom = None
    left, right = w, 0  # right is exclusive
    for y in range(h):
        row = buf[y * bpl:y * bpl + w]
        first = w - len(row.lstrip(b"\0"))
        if first == w:
            continue
        if top is None:
            top = y
        bottom = y
        left = min(left, first)
        right = max(right, len(row.rstrip(b"\0")))
    if top is None:
        return None
    return left, top, right - left, bottom - top + 1


def _log(msg):
    print(f"KMCP_MAO: {msg}", flush=True)


@unittest.skipUnless(os.environ.get(MAO_ENV), f"set {MAO_ENV} to the Mao .ora (acceptance § 1)")
class MaoRebuildTest(EnvelopeAssertions, unittest.TestCase):
    def test_mao_rebuild(self):
        ora = os.path.expanduser(os.environ[MAO_ENV])
        self.assertTrue(os.path.isfile(ora), ora)
        if TOOLS not in sys.path:
            sys.path.insert(0, TOOLS)
        import ora_to_manifest

        tmp = tempfile.mkdtemp(prefix="kmcp-mao-")
        handles = []
        start = time.perf_counter()
        try:
            with temp_config({"allowed_dirs": [tmp]}):
                self._rebuild(ora_to_manifest, ora, tmp, handles)
        finally:
            for handle in handles:
                try:
                    registry.lookup(handle)
                except CommandError:
                    continue  # already closed
                run("close_document", doc=handle, discard=True)
            shutil.rmtree(tmp, ignore_errors=True)
            _log(f"total {time.perf_counter() - start:.1f} s; temp dir removed: "
                 f"{not os.path.exists(tmp)}")
        self.assertFalse(os.path.exists(tmp))

    def _rebuild(self, ora_to_manifest, ora, tmp, handles):
        out = os.path.join(tmp, "mao")
        t = time.perf_counter()
        m = ora_to_manifest.convert(ora, out)
        _log(f"ora_to_manifest {time.perf_counter() - t:.1f} s")
        entries = [e for e in m["layers"] if not e.get("projection")]
        self.assertEqual((m["document"]["width"], m["document"]["height"]), (WIDTH, HEIGHT))

        canvas = self.assertOk(run("new_canvas", width=WIDTH, height=HEIGHT,
                                   background="transparent", name="kmcp-mao"))
        handle = canvas["doc"]
        handles.append(handle)

        envelope = run("create_layers", doc=handle, manifest=os.path.join(out, "manifest.json"))
        result = self.assertOk(envelope)
        _log(f"create_layers elapsed_ms={envelope['elapsed_ms']}")
        self.assertEqual(result["counts"], {"grouplayer": 38, "paintlayer": 184, "total": 222})
        self.assertEqual((result["skipped"], result["warnings"]), ([], []))

        background = canvas["background_layer"]
        layers = self._layers(handle, background)
        self.assertEqual(collections.Counter(l["path"] for l in layers),
                         collections.Counter(e["path"] for e in entries))
        self.assertEqual(sorted(l["path"] for l in layers if l["blend_mode"] == "multiply"),
                         sorted(e["path"] for e in entries if e["blend_mode"] == "multiply"))
        self.assertEqual(sum(1 for l in layers if l["blend_mode"] == "multiply"), 7)
        self.assertEqual(sorted(l["name"] for l in layers
                                if l["type"] == "grouplayer" and not l["visible"]),
                         sorted(HIDDEN_GROUPS))
        self.assertEqual(sorted(l["path"] for l in layers if not l["visible"]),
                         sorted(e["path"] for e in entries if not e["visible"]))

        # Bounds: each paint layer's equals its PNG's opaque bounds, offset.
        t = time.perf_counter()
        by_key = {c["key"]: c["id"] for c in result["created"]}
        by_id = {l["id"]: l for l in layers}
        for e in entries:
            if e["type"] != "paint":
                continue
            got = by_id[by_key[e["key"]]]["bounds"]
            ob = opaque_bounds(os.path.join(out, e["png"])) if e["png"] else None
            want = None if ob is None else {"x": e["x"] + ob[0], "y": e["y"] + ob[1],
                                            "w": ob[2], "h": ob[3]}
            self.assertEqual(got, want, e["path"])
        _log(f"bounds check {time.perf_counter() - t:.1f} s")

        # Save, close, reopen: same counts.
        t = time.perf_counter()
        kra = os.path.join(tmp, "mao.kra")
        saved = self.assertOk(run("save", doc=handle, path=kra))
        self.assertTrue(os.path.isfile(saved["path"]))
        _log(f"save {time.perf_counter() - t:.1f} s, {saved['bytes']} bytes")
        self.assertOk(run("close_document", doc=handle, discard=True))
        t = time.perf_counter()
        reopened = self.assertOk(run("open_file", path=kra))
        handles.append(reopened["doc"])
        _log(f"open {time.perf_counter() - t:.1f} s")
        again = self._layers(reopened["doc"], background)
        self.assertEqual(collections.Counter(l["type"] for l in again),
                         collections.Counter({"paintlayer": 184, "grouplayer": 38}))
        self.assertEqual(collections.Counter(l["path"] for l in again),
                         collections.Counter(e["path"] for e in entries))

    def _layers(self, handle, background):
        """list_layers of ``handle`` without new_canvas's Background layer
        (id ``background``; a .kra keeps layer ids)."""
        listed = self.assertOk(run("list_layers", doc=handle))["layers"]
        rest = [l for l in listed if l["id"] != background]
        self.assertEqual(len(listed) - len(rest), 1)
        return rest
