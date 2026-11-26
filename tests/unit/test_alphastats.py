"""kritamcp.alphastats: the dilation kernel, the shift fallback and the
stray-alpha count, against a brute-force reference (per-pixel loops are
fine in tests, on small buffers)."""

import os
import random
import subprocess
import sys
import types

import pytest

from kritamcp import alphastats as a


def reference_dilate(mask, w, h, r):
    half = a.kernel_half_widths(r)
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            if not mask[y * w + x]:
                continue
            for dy in range(-r, r + 1):
                g = half[abs(dy)]
                for dx in range(-g, g + 1):
                    if 0 <= x + dx < w and 0 <= y + dy < h:
                        out[(y + dy) * w + x + dx] = 1
    return bytes(out)


def reference_stray(alpha, w, h, below, distance):
    strong = bytes(1 if v >= below else 0 for v in alpha)
    grown = reference_dilate(strong, w, h, distance) if distance > 0 else strong
    return sum(1 for i, v in enumerate(alpha) if 0 < v < below and not grown[i])


# Pixels covered when Krita 5.3.4's Selection.grow(r, r) grows one pixel
# (headless probe, 2026-11-25).
KRITA_GROW_COUNTS = {1: 9, 2: 21, 3: 45, 4: 69, 5: 101, 8: 241}


@pytest.mark.parametrize("r,count", sorted(KRITA_GROW_COUNTS.items()))
def test_kernel_matches_krita_grow(r, count):
    assert a.kernel_size(r) == count


def test_kernel_shapes():
    assert a.kernel_half_widths(0) == [0]
    assert a.kernel_half_widths(1) == [1, 1]           # 3x3 square
    assert a.kernel_half_widths(2) == [2, 2, 1]        # 5x5 without corners
    assert a.kernel_half_widths(4) == [4, 4, 4, 3, 2]  # (3, 3), (4, 2) in; (4, 3) out
    assert a.kernel_half_widths(8) == [8, 8, 8, 8, 7, 7, 6, 5, 3]


@pytest.mark.parametrize("r", [1, 2, 3, 4, 5, 8])
def test_dilate_shift_single_pixel(r):
    n = 2 * r + 7
    mask = bytearray(n * n)
    mask[(n // 2) * n + n // 2] = 1
    grown = a.dilate_shift(bytes(mask), n, n, r)
    assert grown.count(1) == a.kernel_size(r)
    assert grown == reference_dilate(mask, n, n, r)


@pytest.mark.parametrize("w,h", [(1, 1), (1, 9), (9, 1), (13, 7), (40, 31)])
@pytest.mark.parametrize("r", [0, 1, 2, 4, 6])
def test_dilate_shift_matches_reference(w, h, r):
    rng = random.Random(w * 1000 + h * 10 + r)
    mask = bytes(1 if rng.random() < 0.05 else 0 for _ in range(w * h))
    assert a.dilate_shift(mask, w, h, r) == reference_dilate(mask, w, h, r)


def test_dilate_shift_edges_do_not_wrap():
    # A pixel at the right edge must not spill into the next row's left edge.
    w, h = 6, 3
    mask = bytearray(w * h)
    mask[0 * w + 5] = 1
    grown = a.dilate_shift(bytes(mask), w, h, 1)
    assert grown == bytes([0, 0, 0, 0, 1, 1,
                           0, 0, 0, 0, 1, 1,
                           0, 0, 0, 0, 0, 0])


@pytest.mark.parametrize("below,distance", [(8, 4), (8, 1), (3, 2), (1, 4), (255, 3), (8, 0)])
def test_stray_count_matches_reference(below, distance):
    rng = random.Random(below * 31 + distance)
    w, h = 30, 22
    values = [0] * 12 + [1, 2, 3, 5, 7, 8, 100, 255]
    alpha = bytes(rng.choice(values) if rng.random() < 0.2 else 0 for _ in range(w * h))
    got, _ = a.stray_count(alpha, w, h, below, distance)
    assert got == reference_stray(alpha, w, h, below, distance)


def test_stray_count_short_cuts():
    calls = []

    def dilate(mask, w, h, r):
        calls.append(r)
        return a.dilate_shift(mask, w, h, r)

    w, h = 4, 1
    assert a.stray_count(bytes([0, 255, 255, 0]), w, h, 8, 4, dilate) == (0, False)  # no weak
    assert a.stray_count(bytes([0, 3, 7, 0]), w, h, 8, 4, dilate) == (2, False)      # no strong
    assert a.stray_count(bytes([3, 255, 0, 0]), w, h, 8, 0, dilate) == (1, False)    # distance 0
    assert calls == []
    assert a.stray_count(bytes([3, 255, 0, 0]), w, h, 8, 1, dilate) == (0, True)
    assert calls == [1]


def test_stray_count_checks_dilation_size():
    with pytest.raises(ValueError):
        a.stray_count(bytes([3, 255]), 2, 1, 8, 1, lambda m, w, h, r: b"\0")


def test_grown_values_any_nonzero():
    # Selection.pixelData gives 0/255: any nonzero byte counts as covered.
    got = a.stray_count(bytes([3, 255, 0, 3]), 4, 1, 8, 1,
                        lambda m, w, h, r: bytes([255, 255, 255, 0]))
    assert got == (1, True)


def test_masks_and_tables():
    alpha = bytes([0, 1, 7, 8, 255])
    assert alpha.translate(a.weak_table(8)) == bytes([0, 1, 1, 0, 0])
    assert alpha.translate(a.strong_table(8)) == bytes([0, 0, 0, 1, 1])
    assert a.to_ff(bytes([0, 1, 1, 0])) == bytes([0, 255, 255, 0])


def test_normalize_settings():
    assert a.normalize_settings(None) == a.DEFAULT_SETTINGS
    assert a.normalize_settings({}) == a.DEFAULT_SETTINGS
    assert a.normalize_settings({"stray_alpha_below": 4, "stray_alpha_distance": 0,
                                 "additive_mode": "linear_dodge", "other": 1}) == {
        "additive_mode": "linear_dodge", "stray_alpha_below": 4, "stray_alpha_distance": 0}
    obj = types.SimpleNamespace(additive_mode="add", stray_alpha_below=16)
    assert a.normalize_settings(obj) == {"additive_mode": "add", "stray_alpha_below": 16,
                                         "stray_alpha_distance": 4}
    for bad in ({"stray_alpha_below": 0}, {"stray_alpha_below": 256}, {"stray_alpha_below": True},
                {"stray_alpha_below": 4.0}, {"stray_alpha_distance": -1},
                {"stray_alpha_distance": a.MAX_DISTANCE + 1}, {"additive_mode": "plus"}):
        assert a.normalize_settings(bad) == a.DEFAULT_SETTINGS, bad
    assert a.normalize_settings({"stray_alpha_below": 4}) is not a.DEFAULT_SETTINGS


def test_pure_import():
    code = ("import sys, kritamcp.alphastats\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('krita', 'PyQt5'))\n"
            "assert not bad, bad\n")
    plugin = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "krita-plugin")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=plugin)
