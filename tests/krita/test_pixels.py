"""kritamcp/pixels.py, headless: byte order and bit-exact round trips
(mirroring spike Q5), opaque bounds, crop, alpha counts, source-over
compositing and PNG saving.

Per-pixel Python loops appear only here, in tests, over small buffers, to
compute expected values independently of the code under test.
"""

import os
import shutil
import stat
import struct
import tempfile
import time
import unittest

from PyQt5.QtGui import QImage

from helpers import add_paint_layer, scratch_document, settle
from kritamcp import pixels
from kritamcp.errors import CommandError

BIG_W, BIG_H = 5800, 8400  # Mao, 48.7 MP


def pattern(w, h, seed=0):
    """BGRA with varied colours and alpha: 0, partial and 255, and colours
    under zero alpha."""
    return bytes((i * 37 + seed * 11 + (i // 4) * 5) % 256 for i in range(4 * w * h))


def solid(w, h, bgra):
    return bytes(bgra) * (w * h)


def put(buf, w, x, y, bgra):
    """``buf`` (bytearray) with pixel (x, y) set to ``bgra``."""
    o = 4 * (y * w + x)
    buf[o:o + 4] = bytes(bgra)


def exact_over(s, d):
    """Source-over of one non-premultiplied BGRA pixel, in floats:
    returns (b, g, r, a) unrounded."""
    sa, da = s[3] / 255, d[3] / 255
    oa = sa + da * (1 - sa)
    if oa == 0:
        return (0.0, 0.0, 0.0, 0.0)
    rgb = tuple((s[c] * sa + d[c] * da * (1 - sa)) / oa for c in range(3))
    return rgb + (oa * 255,)


def idat_flevel(path):
    """The FLEVEL bits of the first IDAT's zlib header: 0 (zlib level 0-1),
    1 (2-5), 2 (6, the default) or 3 (7-9)."""
    with open(path, "rb") as f:
        data = f.read()
    pos = 8
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        if ctype == b"IDAT":
            return data[pos + 9] >> 6  # FLG, the zlib header's second byte
        pos += 12 + length
    raise AssertionError(f"no IDAT in {path}")


class ByteOrderTest(unittest.TestCase):
    """Q5 mirror: BGRA through setPixelData/pixelData, QImage and PNG."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kmcp-pixels-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_layer_qimage_png_round_trip(self):
        w, h = 7, 5
        data = bytearray(pattern(w, h))
        put(data, w, 0, 0, (10, 20, 30, 40))      # partial alpha
        put(data, w, 1, 0, (200, 150, 100, 0))    # colour under zero alpha
        put(data, w, 2, 0, (1, 2, 3, 255))
        data = bytes(data)
        with scratch_document(16, 16, name="kmcp-pixels-q5") as doc:
            node = add_paint_layer(doc)
            self.assertTrue(node.setPixelData(data, 3, 4, w, h))
            settle(doc)
            back = pixels.as_bytes(node.pixelData(3, 4, w, h))
        self.assertEqual(back, data)

        image = pixels.wrap(back, w, h)
        self.assertEqual(image.format(), QImage.Format_ARGB32)
        # QImage.pixel is 0xAARRGGBB: bytes are B, G, R, A in memory.
        self.assertEqual(image.pixel(0, 0), 0x281E140A)
        self.assertEqual(image.pixel(1, 0), 0x006496C8)
        self.assertEqual(image.pixel(2, 0), 0xFF030201)

        owned = pixels.to_qimage(back, w, h)
        self.assertEqual(pixels.from_qimage(owned), (data, w, h))

        for level in (None, 0, 9):
            path = os.path.join(self.dir, f"q5-{level}.png")
            size = pixels.save_png(back, path, w, h, level=level)
            self.assertEqual(size, os.path.getsize(path))
            self.assertEqual(pixels.load_png_bgra(path), (data, w, h), level)
            self.assertEqual(pixels.png_size(path), (w, h))
        path = os.path.join(self.dir, "from-image.png")
        pixels.save_png(owned, path)
        self.assertEqual(pixels.load_png_bgra(path)[0], data)

    def test_from_qimage_drops_row_padding_and_converts(self):
        w, h, stride = 3, 4, 4 * 3 + 8
        rows = [pattern(w, 1, seed=y) for y in range(h)]
        padded = b"".join(r + b"\xEE" * 8 for r in rows)
        image = QImage(padded, w, h, stride, QImage.Format_ARGB32)
        self.assertEqual(image.bytesPerLine(), stride)
        self.assertEqual(pixels.from_qimage(image), (b"".join(rows), w, h))
        rgb = QImage(2, 2, QImage.Format_RGB888)
        rgb.fill(0x112233)
        self.assertEqual(pixels.from_qimage(rgb)[0], bytes((0x33, 0x22, 0x11, 0xFF)) * 4)

    def test_buffer_checks(self):
        with self.assertRaises(ValueError):
            pixels.wrap(bytes(15), 2, 2)
        with self.assertRaises(ValueError):
            pixels.wrap(bytes(0), 0, 0)
        self.assertEqual(pixels.alpha(bytes((1, 2, 3, 4, 5, 6, 7, 8))), bytes((4, 8)))


class OpaqueBoundsTest(unittest.TestCase):
    def test_single_pixel_at_each_corner_and_inside(self):
        w, h = 7, 5
        for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (3, 2)):
            buf = bytearray(4 * w * h)
            put(buf, w, x, y, (0, 0, 0, 1))
            self.assertEqual(pixels.opaque_bounds(buf, w, h), (x, y, 1, 1), (x, y))
        buf = bytearray(4 * w * h)
        put(buf, w, 1, 3, (0, 0, 0, 9))
        put(buf, w, 5, 1, (0, 0, 0, 255))
        self.assertEqual(pixels.opaque_bounds(buf, w, h), (1, 1, 5, 3))

    def test_full_empty_and_zero_alpha_colours(self):
        w, h = 4, 3
        self.assertEqual(pixels.opaque_bounds(solid(w, h, (1, 2, 3, 255)), w, h), (0, 0, w, h))
        self.assertIsNone(pixels.opaque_bounds(bytes(4 * w * h), w, h))
        self.assertIsNone(pixels.opaque_bounds(solid(w, h, (255, 255, 255, 0)), w, h))
        self.assertEqual(pixels.opaque_bounds(solid(1, 1, (0, 0, 0, 7)), 1, 1), (0, 0, 1, 1))

    def test_threshold(self):
        w, h = 6, 6
        buf = bytearray(4 * w * h)
        put(buf, w, 0, 0, (0, 0, 0, 10))
        put(buf, w, 2, 3, (0, 0, 0, 100))
        put(buf, w, 4, 1, (0, 0, 0, 200))
        self.assertEqual(pixels.opaque_bounds(buf, w, h), (0, 0, 5, 4))
        self.assertEqual(pixels.opaque_bounds(buf, w, h, threshold=11), (2, 1, 3, 3))
        self.assertEqual(pixels.opaque_bounds(buf, w, h, threshold=100), (2, 1, 3, 3))
        self.assertEqual(pixels.opaque_bounds(buf, w, h, threshold=101), (4, 1, 1, 1))
        self.assertIsNone(pixels.opaque_bounds(buf, w, h, threshold=201))
        for bad in (0, 256, 1.5, True):
            with self.assertRaises(ValueError):
                pixels.opaque_bounds(buf, w, h, threshold=bad)

    def test_matches_brute_force(self):
        w, h = 13, 9
        for seed in range(6):
            data = bytearray(4 * w * h)
            data[3::4] = bytes(((i * 7919 + seed * 104729) % 97 > 90) * 255 for i in range(w * h))
            pts = [(i % w, i // w) for i in range(w * h) if data[4 * i + 3]]
            want = None
            if pts:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                want = (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
            self.assertEqual(pixels.opaque_bounds(data, w, h), want, seed)

    def test_timing_at_mao_size(self):
        n = BIG_W * BIG_H
        sparse = bytearray(4 * n)
        put(sparse, BIG_W, 1, 1, (9, 9, 9, 255))  # top-left and bottom-right: every row is scanned
        put(sparse, BIG_W, BIG_W - 2, BIG_H - 2, (9, 9, 9, 3))
        sparse = bytes(sparse)
        full = solid(BIG_W, BIG_H, (1, 2, 3, 255))
        timings = {}
        for label, data, want in (("sparse", sparse, (1, 1, BIG_W - 2, BIG_H - 2)),
                                  ("full", full, (0, 0, BIG_W, BIG_H)),
                                  ("empty", bytes(4 * n), None)):
            for threshold in (1, 128):
                t0 = time.perf_counter()
                got = pixels.opaque_bounds(data, BIG_W, BIG_H, threshold=threshold)
                timings[(label, threshold)] = time.perf_counter() - t0
                if label == "sparse" and threshold == 128:
                    self.assertEqual(got, (1, 1, 1, 1))
                else:
                    self.assertEqual(got, want, (label, threshold))
        t0 = time.perf_counter()
        count = pixels.count_alpha(full, 128)
        timings[("count_alpha full", 128)] = time.perf_counter() - t0
        self.assertEqual(count, n)
        print("\n[timing] opaque_bounds at %dx%d: %s" % (BIG_W, BIG_H, ", ".join(
            f"{label} t={thr} {secs * 1000:.0f} ms" for (label, thr), secs in timings.items())))
        for key, secs in timings.items():
            self.assertLess(secs, 1.0, key)


class CropCountTest(unittest.TestCase):
    def test_crop(self):
        w, h = 6, 5
        data = pattern(w, h)

        def brute(x, y, cw, ch):
            return b"".join(data[4 * ((y + r) * w + x):4 * ((y + r) * w + x + cw)]
                            for r in range(ch))

        for rect in ((0, 0, w, h), (0, 1, w, 3), (2, 1, 3, 2), (5, 4, 1, 1), (0, 0, 1, h)):
            self.assertEqual(pixels.crop(data, w, h, rect), brute(*rect), rect)
        for rect in ((-1, 0, 2, 2), (0, 0, w + 1, 1), (5, 4, 2, 1), (0, 0, 0, 1)):
            with self.assertRaises(ValueError):
                pixels.crop(data, w, h, rect)

    def test_count_alpha(self):
        data = b"".join(bytes((9, 9, 9, a)) for a in (0, 1, 2, 127, 128, 255, 0))
        self.assertEqual(pixels.count_alpha(data), 5)
        self.assertEqual(pixels.count_alpha(data, 2), 4)
        self.assertEqual(pixels.count_alpha(data, 128), 2)
        self.assertEqual(pixels.count_alpha(data, 255), 1)
        self.assertEqual(pixels.count_alpha(bytes(40)), 0)
        with self.assertRaises(ValueError):
            pixels.count_alpha(data, 0)


class CompositeOverTest(unittest.TestCase):
    def grid(self):
        """256x256: pixel (i, j) has src alpha i and dst alpha j, with
        colours that vary along both axes."""
        w = h = 256
        src, dst = bytearray(4 * w * h), bytearray(4 * w * h)
        for j in range(h):
            for i in range(w):
                o = 4 * (j * w + i)
                src[o:o + 4] = bytes(((i * 7 + j * 3) % 256, (i * 13 + 50) % 256,
                                      (j * 11 + 90) % 256, i))
                dst[o:o + 4] = bytes(((j * 5 + i) % 256, (i * 3 + j * 17) % 256,
                                      (200 - j) % 256, j))
        return bytes(src), bytes(dst), w, h

    def test_against_exact_formula(self):
        src, dst, w, h = self.grid()
        t0 = time.perf_counter()
        out = pixels.composite_over(dst, src, w, h)
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(out), len(src))
        # Worst colour error per result-alpha band, and the worst alpha error.
        bands = ((1, 7), (8, 31), (32, 63), (64, 127), (128, 254), (255, 255))
        worst = {b: 0 for b in bands}
        worst_alpha = 0
        for p in range(w * h):
            o = 4 * p
            s, d, got = src[o:o + 4], dst[o:o + 4], out[o:o + 4]
            sa, da = s[3], d[3]
            if sa == 0:
                self.assertEqual(got, d, p)  # untouched, colour under zero alpha included
                continue
            if sa == 255 or da == 0:
                self.assertEqual(got, s, p)
                continue
            exact = exact_over(s, d)
            worst_alpha = max(worst_alpha, abs(got[3] - exact[3]))
            err = max(abs(got[c] - exact[c]) for c in range(3))
            band = next(b for b in bands if b[0] <= round(exact[3]) <= b[1])
            worst[band] = max(worst[band], err)
        print("\n[composite_over] 256x256 in %.0f ms; worst |alpha error| %.2f; worst |colour "
              "error| by result alpha: %s" % (elapsed * 1000, worst_alpha, ", ".join(
                  f"{lo}-{hi}: {err:.2f}" for (lo, hi), err in worst.items())))
        self.assertLessEqual(worst_alpha, 1.0)
        # Within one level of the exact value everywhere (16-bit working
        # image; measured under 0.9 with Krita 5.3.4's Qt 5.15.7).
        for band, err in worst.items():
            self.assertLess(err, 1.0, band)

    def test_strips_match_one_pass(self):
        src, dst, w, h = self.grid()
        whole = pixels.composite_over(dst, src, w, h)
        saved = pixels._STRIP_PIXELS
        pixels._STRIP_PIXELS = 3 * w + 5  # 3 rows per strip, the last one short
        try:
            self.assertEqual(pixels.composite_over(dst, src, w, h), whole)
        finally:
            pixels._STRIP_PIXELS = saved

    def test_timing_at_mao_size(self):
        n = BIG_W * BIG_H
        dst = bytes((10, 20, 30, 200)) * n
        # Mixed rows: a quarter of each row transparent, the rest partial.
        row = bytes((90, 80, 70, 0)) * (BIG_W // 4) + bytes((90, 80, 70, 128)) * (BIG_W - BIG_W // 4)
        src = row * BIG_H
        t0 = time.perf_counter()
        out = pixels.composite_over(dst, src, BIG_W, BIG_H)
        secs = time.perf_counter() - t0
        print(f"\n[composite_over] {BIG_W}x{BIG_H} mixed rows: {secs * 1000:.0f} ms")
        self.assertEqual(out[:4], dst[:4])
        self.assertEqual(len(out), 4 * n)
        self.assertLess(secs, 20.0)

    def test_exact_cases(self):
        w, h = 4, 1
        dst = b"".join(bytes(p) for p in ((10, 20, 30, 0), (40, 50, 60, 1), (70, 80, 90, 128),
                                          (5, 6, 7, 3)))
        src = b"".join(bytes(p) for p in ((200, 100, 50, 1), (0, 0, 0, 0), (1, 2, 3, 255),
                                          (250, 251, 252, 0)))
        out = pixels.composite_over(dst, src, w, h)
        self.assertEqual(out, src[:4] + dst[4:8] + src[8:12] + dst[12:16])
        # Opaque source everywhere: the source; transparent source: the destination.
        d = pattern(9, 7, 1)
        self.assertEqual(pixels.composite_over(d, solid(9, 7, (1, 2, 3, 255)), 9, 7),
                         solid(9, 7, (1, 2, 3, 255)))
        self.assertEqual(pixels.composite_over(d, solid(9, 7, (1, 2, 3, 0)), 9, 7), d)
        # Onto a transparent destination: the source, except that where the
        # source is transparent too the destination stays.
        s = pattern(9, 7, 2)
        want = bytearray(s)
        for p in range(63):
            if s[4 * p + 3] == 0:
                want[4 * p:4 * p + 4] = bytes(4)
        self.assertEqual(pixels.composite_over(bytes(4 * 63), s, 9, 7), bytes(want))
        with self.assertRaises(ValueError):
            pixels.composite_over(d, s[:-4], 9, 7)


class SavePngTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kmcp-savepng-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_quality_mapping(self):
        # qpnghandler.cpp (Qt 5.15): level = (100 - quality) * 9 // 91.
        self.assertEqual(pixels.png_quality(None), -1)
        self.assertEqual(pixels.png_quality(0), 100)
        self.assertEqual(pixels.png_quality(9), 9)
        for level in range(10):
            q = pixels.png_quality(level)
            self.assertEqual((100 - q) * 9 // 91, level, level)
            if q < 100:
                self.assertNotEqual((100 - (q + 1)) * 9 // 91, level, level)  # the largest such q
        for bad in (-1, 10, 1.0, True, "6"):
            with self.assertRaises(ValueError):
                pixels.png_quality(bad)

    def test_levels_reach_zlib(self):
        # The IDAT's zlib header records the level band zlib was given.
        w, h = 64, 64
        data = pattern(w, h)
        want = {0: 0, 1: 0, 2: 1, 5: 1, 6: 2, 7: 3, 9: 3, None: 2}
        for level, flevel in want.items():
            path = os.path.join(self.dir, f"l{level}.png")
            pixels.save_png(data, path, w, h, level=level)
            self.assertEqual(idat_flevel(path), flevel, level)

    def test_level_size_and_speed(self):
        w, h = 2048, 2048
        row = b"".join(bytes(((x * 7) % 256, x % 256, (x // 8) % 256, 255)) for x in range(w))
        # Rows shift, so the image compresses but not trivially.
        data = b"".join(row[(4 * y) % len(row):] + row[:(4 * y) % len(row)] for y in range(h))
        stats = {}
        for level in (0, None, 9):
            path = os.path.join(self.dir, f"big-{level}.png")
            t0 = time.perf_counter()
            size = pixels.save_png(data, path, w, h, level=level)
            stats[level] = (size, time.perf_counter() - t0)
        print("\n[save_png] 2048x2048: " + ", ".join(
            f"level {lv}: {size} bytes {secs * 1000:.0f} ms" for lv, (size, secs) in stats.items()))
        self.assertGreater(stats[0][0], stats[None][0])
        self.assertLessEqual(stats[9][0], stats[None][0])
        self.assertLess(stats[0][1], stats[9][1])

    def test_replaces_atomically_and_leaves_no_temp(self):
        path = os.path.join(self.dir, "a.png")
        pixels.save_png(solid(2, 2, (1, 2, 3, 4)), path, 2, 2)
        pixels.save_png(solid(3, 1, (5, 6, 7, 8)), path, 3, 1)
        self.assertEqual(os.listdir(self.dir), ["a.png"])
        self.assertEqual(pixels.load_png_bgra(path), (solid(3, 1, (5, 6, 7, 8)), 3, 1))

    def test_failure_leaves_no_file(self):
        missing = os.path.join(self.dir, "nope", "a.png")
        with self.assertRaises(CommandError) as cm:
            pixels.save_png(solid(2, 2, (1, 2, 3, 4)), missing, 2, 2)
        self.assertEqual(cm.exception.code, "krita_error")
        self.assertFalse(os.path.exists(os.path.dirname(missing)))

        path = os.path.join(self.dir, "keep.png")
        pixels.save_png(solid(2, 2, (1, 2, 3, 4)), path, 2, 2)
        before = open(path, "rb").read()
        os.chmod(self.dir, stat.S_IRUSR | stat.S_IXUSR)  # no new files in the dir
        try:
            with self.assertRaises(CommandError) as cm:
                pixels.save_png(solid(5, 5, (9, 9, 9, 9)), path, 5, 5)
            self.assertEqual(cm.exception.code, "krita_error")
        finally:
            os.chmod(self.dir, stat.S_IRWXU)
        self.assertEqual(os.listdir(self.dir), ["keep.png"])
        self.assertEqual(open(path, "rb").read(), before)

        with self.assertRaises(CommandError):
            pixels.save_png(QImage(), os.path.join(self.dir, "null.png"))
        self.assertEqual(os.listdir(self.dir), ["keep.png"])


class RequireRgba8Test(unittest.TestCase):
    def test_guard(self):
        with scratch_document(4, 4, name="kmcp-pixels-rgba8") as doc:
            pixels.require_rgba8(doc)
        with scratch_document(4, 4, name="kmcp-pixels-u16", color_depth="U16") as doc:
            with self.assertRaises(CommandError) as cm:
                pixels.require_rgba8(doc)
        self.assertEqual(cm.exception.code, "conflict")
        self.assertEqual(cm.exception.details, {"color_model": "RGBA", "color_depth": "U16"})


if __name__ == "__main__":
    unittest.main()
