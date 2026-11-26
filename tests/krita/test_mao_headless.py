"""Acceptance tests 1 (Mao rebuild, P2.4) and 2 (round trip, Mao variant,
P3.2; compared through ``render`` since P3.4), headless.

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
from kritamcp import pixels, registry
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


def _describe_diff(a, b, w, h):
    """Where two BGRA buffers differ: the differing rows and columns, the
    differing pixel count, how many of those have alpha 0 in both, and a
    few samples. Row-wise ``bytes`` work; per-pixel loops only over
    differing pixels of a few rows."""
    stride = 4 * w
    rows, cols, count, zero_both, samples = [], [w, -1], 0, 0, []
    for y in range(h):
        ra, rb = a[y * stride:(y + 1) * stride], b[y * stride:(y + 1) * stride]
        if ra == rb:
            continue
        rows.append(y)
        diff = (int.from_bytes(ra, "big") ^ int.from_bytes(rb, "big")).to_bytes(stride, "big")
        first = (stride - len(diff.lstrip(b"\0"))) // 4
        last = (len(diff.rstrip(b"\0")) - 1) // 4
        cols = [min(cols[0], first), max(cols[1], last)]
        for x in range(first, last + 1):
            pa, pb = ra[4 * x:4 * x + 4], rb[4 * x:4 * x + 4]
            if pa != pb:
                count += 1
                zero_both += pa[3] == 0 and pb[3] == 0
                if len(samples) < 8:
                    samples.append(((x, y), tuple(pa), tuple(pb)))
        if count > 2_000_000:
            break
    if not rows:
        return "identical"
    return (f"rows {rows[0]}..{rows[-1]} ({len(rows)} rows), cols {cols[0]}..{cols[1]}, "
            f"{count} pixels differ, {zero_both} of them alpha 0 in both; samples {samples}")


def _zero_alpha0_colour(row):
    """``row`` (BGRA bytes) with the colour of every alpha-0 pixel zeroed."""
    keep = row[3::4].translate(bytes([0] + [0xFF] * 255))
    mask = bytearray(len(row))
    for c in range(4):
        mask[c::4] = keep
    return (int.from_bytes(row, "big") & int.from_bytes(mask, "big")).to_bytes(len(row), "big")


def _same_but_alpha0_colour(a, b, w, h):
    """True if BGRA buffers ``a`` and ``b`` differ at most in the colour
    bytes of pixels whose alpha is 0 in both. Row-wise ``bytes`` work."""
    stride = 4 * w
    for y in range(h):
        ra, rb = a[y * stride:(y + 1) * stride], b[y * stride:(y + 1) * stride]
        if ra != rb and _zero_alpha0_colour(ra) != _zero_alpha0_colour(rb):
            return False
    return True


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
        self._round_trip(tmp, reopened["doc"], handles)

    def _render(self, handle, path, **params):
        """Render ``handle`` to ``path``; returns ``(result, elapsed_ms)``."""
        envelope = run("render", doc=handle, path=path, **params)
        return self.assertOk(envelope), envelope["elapsed_ms"]

    def _round_trip(self, tmp, handle, handles):
        """Acceptance § 2 (Mao variant), headless: render the reopened
        document, export it, rebuild that export on a third canvas, render
        that, and compare the two renders (colour under zero alpha
        excepted)."""
        # Right after open_file: render reads the projection with only
        # waitForDone(); check that against a refreshed projection.
        a_png = os.path.join(tmp, "renders", "a.png")
        a_result, ms = self._render(handle, a_png)
        _log(f"render scale 1 elapsed_ms={ms} bytes={a_result['bytes']}")
        small, ms = self._render(handle, os.path.join(tmp, "renders", "a-quarter.png"), scale=0.25)
        self.assertEqual((small["width"], small["height"]), (WIDTH // 4, HEIGHT // 4))
        _log(f"render scale 0.25 elapsed_ms={ms} bytes={small['bytes']}")
        _, ms = self._render(handle, os.path.join(tmp, "renders", "a-level1.png"), png_level=1)
        _log(f"render scale 1 png_level 1 elapsed_ms={ms}")
        a = pixels.load_png_bgra(a_png)[0]
        self.assertEqual(len(a), 4 * WIDTH * HEIGHT)
        doc = registry.lookup(handle)
        t = time.perf_counter()
        doc.refreshProjection()
        doc.waitForDone()
        _log(f"refreshProjection + waitForDone {time.perf_counter() - t:.2f} s")
        fresh = pixels.as_bytes(doc.pixelData(0, 0, WIDTH, HEIGHT))
        current = a == fresh
        del doc, fresh
        _log(f"render right after open_file == refreshed projection: {current}")
        self.assertTrue(current, "render after open_file read a stale projection")
        # A subset render goes through a clone (D10 B).
        top = self.assertOk(run("list_layers", doc=handle, depth=1))["layers"]
        group = next(l for l in top if l["type"] == "grouplayer" and l["visible"])
        subset, ms = self._render(handle, os.path.join(tmp, "renders", "a-subset.png"),
                                  only=[group["id"]], scale=0.25)
        self.assertEqual(subset["only"], [group["id"]])
        _log(f"render only=[top-level group] scale 0.25 elapsed_ms={ms}")

        export_dir = os.path.join(tmp, "export")
        envelope = run("export_layers", doc=handle, dir=export_dir, png_level=1)
        exported = self.assertOk(envelope)
        _log(f"export_layers elapsed_ms={envelope['elapsed_ms']} count={exported['count']} "
             f"bytes={exported['bytes']}")
        self.assertEqual((exported["skipped"], exported["warnings"]), ([], []))
        self.assertEqual(exported["count"], 184)  # every paint layer has pixels; Background none

        canvas = self.assertOk(run("new_canvas", width=WIDTH, height=HEIGHT,
                                   background="transparent", name="kmcp-mao-3"))
        handles.append(canvas["doc"])
        envelope = run("create_layers", doc=canvas["doc"], manifest=exported["manifest"])
        rebuilt = self.assertOk(envelope)
        _log(f"create_layers from export elapsed_ms={envelope['elapsed_ms']}")
        self.assertEqual(rebuilt["counts"], {"grouplayer": 38, "paintlayer": 185, "total": 223})

        fields = ("path", "type", "visible", "opacity", "blend_mode", "inherit_alpha",
                  "alpha_locked", "locked", "pass_through", "bounds")

        def tree(h, drop=None):
            listed = self.assertOk(run("list_layers", doc=h))["layers"]
            return [tuple(l[k] for k in fields) for l in listed if l["id"] != drop]

        self.assertEqual(tree(canvas["doc"], drop=canvas["background_layer"]), tree(handle))

        b_png = os.path.join(tmp, "renders", "b.png")
        _, ms = self._render(canvas["doc"], b_png)
        _log(f"render of the rebuild, scale 1 elapsed_ms={ms}")
        t = time.perf_counter()
        b = pixels.load_png_bgra(b_png)[0]
        same = a == b
        _log(f"render compare {time.perf_counter() - t:.1f} s; byte-identical: {same}")
        if not same:
            _log("render diff: " + _describe_diff(a, b, WIDTH, HEIGHT))
        # Known difference (P3.2, 2026-11-25): some Mao PNGs (from the .ora)
        # have coloured alpha-0 margins outside the layer's alpha bounds,
        # and Krita's projection keeps such colour at fully transparent
        # pixels (11 pixels in rows 4728-5151; e.g. x=1524, Thumb R's 1-px
        # margin). export_layers crops to alpha > 0, so the rebuild has
        # (0, 0, 0, 0) there. Every pixel with alpha > 0, and alpha
        # everywhere, must match (acceptance § 2).
        self.assertTrue(_same_but_alpha0_colour(a, b, WIDTH, HEIGHT),
                        "the rebuilt Mao renders differently from the exported one")

    def _layers(self, handle, background):
        """list_layers of ``handle`` without new_canvas's Background layer
        (id ``background``; a .kra keeps layer ids)."""
        listed = self.assertOk(run("list_layers", doc=handle))["layers"]
        rest = [l for l in listed if l["id"] != background]
        self.assertEqual(len(listed) - len(rest), 1)
        return rest
