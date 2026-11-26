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
    assert a.DEFAULT_SETTINGS == {"stray_alpha_below": 8, "stray_alpha_distance": 4,
                                  "stray_speck_max_px": 64}
    assert a.normalize_settings(None) == a.DEFAULT_SETTINGS
    assert a.normalize_settings({}) == a.DEFAULT_SETTINGS
    assert a.normalize_settings({"stray_alpha_below": 4, "stray_alpha_distance": 0,
                                 "additive_mode": "linear_dodge", "stray_speck_max_px": 1,
                                 "other": 1}) == {
        "stray_alpha_below": 4, "stray_alpha_distance": 0, "stray_speck_max_px": 1}
    obj = types.SimpleNamespace(additive_mode="add", stray_alpha_below=16)
    assert a.normalize_settings(obj) == {"stray_alpha_below": 16, "stray_alpha_distance": 4,
                                         "stray_speck_max_px": 64}
    assert a.normalize_settings({"stray_speck_max_px": a.MAX_SPECK_PX})["stray_speck_max_px"] == \
        a.MAX_SPECK_PX
    for bad in ({"stray_alpha_below": 0}, {"stray_alpha_below": 256}, {"stray_alpha_below": True},
                {"stray_alpha_below": 4.0}, {"stray_alpha_distance": -1},
                {"stray_alpha_distance": a.MAX_DISTANCE + 1},
                {"stray_speck_max_px": 0}, {"stray_speck_max_px": a.MAX_SPECK_PX + 1},
                {"stray_speck_max_px": 64.0}, {"stray_speck_max_px": False}):
        assert a.normalize_settings(bad) == a.DEFAULT_SETTINGS, bad
    assert a.normalize_settings({"stray_alpha_below": 4}) is not a.DEFAULT_SETTINGS


def test_pure_import():
    code = ("import sys, kritamcp.alphastats\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('krita', 'PyQt5'))\n"
            "assert not bad, bad\n")
    plugin = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "krita-plugin")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=plugin)


# --- specks: connected components of stray pixels (P5.10) --------------

def reference_components(mask, w, h):
    """[(size, (x0, y0, x1, y1))] of the 8-connected components, by flood fill."""
    seen = bytearray(w * h)
    out = []
    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        seen[start] = 1
        stack, size = [start], 0
        x0, y0, x1, y1 = w, h, -1, -1
        while stack:
            i = stack.pop()
            size += 1
            x, y = i % w, i // w
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        j = ny * w + nx
                        if mask[j] and not seen[j]:
                            seen[j] = 1
                            stack.append(j)
        out.append((size, (x0, y0, x1, y1)))
    return out


def grid(rows):
    """A 0/1 mask from strings ('#' = 1): (bytes, w, h)."""
    w, h = len(rows[0]), len(rows)
    return bytes(1 if c == "#" else 0 for row in rows for c in row), w, h


def rect(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def test_components_empty_mask():
    assert a.components(bytes(12), 4, 3, 64) == {"specks": 0, "speck_pixels": 0, "regions": 0,
                                                 "samples": [], "bounds": None,
                                                 "truncated": False}
    assert a.components(b"", 0, 0, 64)["specks"] == 0


def test_components_single_pixel():
    m, w, h = grid(["....", "..#.", "...."])
    assert a.components(m, w, h, 64) == {"specks": 1, "speck_pixels": 1, "regions": 0,
                                         "samples": [rect(2, 1, 1, 1)], "bounds": rect(2, 1, 1, 1),
                                         "truncated": False}


def test_components_diagonal_chain_is_one_component():
    m, w, h = grid(["#....", ".#...", "..#..", "...#.", "....#"])
    r = a.components(m, w, h, 64)
    assert (r["specks"], r["speck_pixels"], r["samples"]) == (1, 5, [rect(0, 0, 5, 5)])
    m, w, h = grid(["....#", "...#.", "..#..", ".#...", "#...."])  # the other diagonal
    assert a.components(m, w, h, 64)["samples"] == [rect(0, 0, 5, 5)]


def test_components_one_pixel_gap_separates():
    m, w, h = grid(["#.#", "...", "#.#"])
    r = a.components(m, w, h, 64)
    assert (r["specks"], r["speck_pixels"]) == (4, 4)
    assert r["samples"] == [rect(0, 0, 1, 1), rect(2, 0, 1, 1), rect(0, 2, 1, 1), rect(2, 2, 1, 1)]


def test_components_two_separate_specks():
    m, w, h = grid(["##......", "##......", "......#.", ".....###"])
    assert a.components(m, w, h, 64) == {"specks": 2, "speck_pixels": 8, "regions": 0,
                                         "samples": [rect(0, 0, 2, 2), rect(5, 2, 3, 2)],
                                         "bounds": rect(0, 0, 8, 4), "truncated": False}


def test_components_big_region_vs_small_specks():
    m, w, h = grid(["#.........",
                    "...#######",
                    "...#######",
                    "...#######",
                    "...#######",
                    "#.........",
                    "..........",
                    "......#..."])
    r = a.components(m, w, h, 4)
    # The 7x4 block is one region; the lone pixels are specks.
    assert (r["specks"], r["speck_pixels"], r["regions"]) == (3, 3, 1)
    assert r["bounds"] == rect(0, 0, 7, 8)  # the specks only, not the region
    assert r["samples"] == [rect(0, 0, 1, 1), rect(0, 5, 1, 1), rect(6, 7, 1, 1)]


def test_components_u_shape_merges_later():
    # The two arms are separate runs until the bottom row joins them.
    m, w, h = grid(["#...#", "#...#", "#...#", "#####"])
    r = a.components(m, w, h, 64)
    assert (r["specks"], r["speck_pixels"], r["samples"]) == (1, 11, [rect(0, 0, 5, 4)])
    # A W: three arms joined by one run, then a run touching the merged root again.
    m, w, h = grid(["#.#.#", "#.#.#", "#####", "..#.."])
    r = a.components(m, w, h, 64)
    assert (r["specks"], r["speck_pixels"], r["samples"]) == (1, 12, [rect(0, 0, 5, 4)])
    # Merged by a diagonal touch only.
    m, w, h = grid(["#...#", ".#.#.", "..#.."])
    assert a.components(m, w, h, 64)["specks"] == 1


def test_components_max_size_boundary():
    m, w, h = grid(["###.", "....", "##.."])
    assert a.components(m, w, h, 3) == {"specks": 2, "speck_pixels": 5, "regions": 0,
                                        "samples": [rect(0, 0, 3, 1), rect(0, 2, 2, 1)],
                                        "bounds": rect(0, 0, 3, 3), "truncated": False}
    r = a.components(m, w, h, 2)
    assert (r["specks"], r["speck_pixels"], r["regions"]) == (1, 2, 1)
    assert a.components(m, w, h, 1)["regions"] == 2


def test_components_samples_capped_and_ordered():
    m, w, h = grid(["......#", "#.#.#..", ".......", "#.#.#.#"])
    r = a.components(m, w, h, 64)
    assert r["specks"] == 8 and len(r["samples"]) == a.MAX_SAMPLES == 5
    assert r["samples"] == [rect(6, 0, 1, 1), rect(0, 1, 1, 1), rect(2, 1, 1, 1), rect(4, 1, 1, 1),
                            rect(0, 3, 1, 1)]


def test_components_rows_do_not_wrap():
    m, w, h = grid(["...#", "#...", "...."])
    assert a.components(m, w, h, 64)["specks"] == 2


def test_components_truncation():
    m, w, h = grid(["#.#.#", ".....", "#.#.#", ".....", "#.#.#"])
    r = a.components(m, w, h, 64, max_runs=4)
    assert r["truncated"] is True
    assert r["specks"] == 6  # stopped after the row that took it past 4 runs
    assert a.components(m, w, h, 64, max_runs=9)["truncated"] is False
    assert a.components(m, w, h, 64, max_runs=9)["specks"] == 9


@pytest.mark.parametrize("seed", range(16))
def test_components_match_flood_fill(seed):
    rng = random.Random(seed)
    w, h = rng.randint(1, 40), rng.randint(1, 30)
    density = rng.choice([0.05, 0.2, 0.4, 0.6])
    m = bytes(1 if rng.random() < density else 0 for _ in range(w * h))
    max_size = rng.choice([1, 3, 10, 64])
    ref = reference_components(m, w, h)
    specks = sorted((bb for size, bb in ref if size <= max_size), key=lambda bb: (bb[1], bb[0]))
    r = a.components(m, w, h, max_size)
    assert r["specks"] == len(specks)
    assert r["speck_pixels"] == sum(size for size, _ in ref if size <= max_size)
    assert r["regions"] == sum(1 for size, _ in ref if size > max_size)
    # Two components never share a top-left-most pixel, so the order is unique.
    assert [(s["x"], s["y"], s["x"] + s["w"] - 1, s["y"] + s["h"] - 1) for s in r["samples"]] == \
        specks[:a.MAX_SAMPLES]
    assert r["truncated"] is False


def test_components_one_megapixel_is_fast():
    import time
    rng = random.Random(7)
    w = h = 1000
    m = bytearray(1 if rng.random() < 0.12 else 0 for _ in range(w * h))  # specks everywhere
    for y in range(300, 700):  # plus a solid glow-like region
        m[y * w + 300:y * w + 700] = b"\x01" * 400
    m = bytes(m)
    t = time.perf_counter()
    r = a.components(m, w, h, 64)
    elapsed = time.perf_counter() - t
    assert elapsed < 1.0, elapsed
    assert r["regions"] >= 1 and r["specks"] > 10000 and not r["truncated"]


def test_stray_mask():
    weak = bytes([1, 1, 0, 1, 0])
    assert a.stray_mask(weak, bytes([0, 255, 255, 0, 0])) == bytes([1, 0, 0, 1, 0])
    assert a.stray_mask(weak, bytes([0, 1, 0, 7, 0])) == bytes([1, 0, 0, 0, 0])
    assert a.stray_mask(bytes([1, 0, 0]), bytes(3)) == bytes([1, 0, 0])  # leading byte kept
    assert a.stray_mask(bytes([0, 0, 1]), bytes(3)) == bytes([0, 0, 1])
    assert a.stray_mask(b"", b"") == b""
    with pytest.raises(ValueError):
        a.stray_mask(weak, bytes(4))


@pytest.mark.parametrize("below,distance", [(8, 4), (8, 1), (3, 2), (8, 0)])
def test_stray_stats_matches_stray_count_and_reference(below, distance):
    rng = random.Random(below * 7 + distance)
    w, h = 30, 22
    values = [0] * 12 + [1, 2, 3, 5, 7, 8, 100, 255]
    alpha = bytes(rng.choice(values) if rng.random() < 0.2 else 0 for _ in range(w * h))
    count, specks, _ = a.stray_stats(alpha, w, h, below, distance, 3, origin=(100, 200))
    assert count == a.stray_count(alpha, w, h, below, distance)[0] == reference_stray(alpha, w, h, below,
                                                                                       distance)
    strong = bytes(1 if v >= below else 0 for v in alpha)
    grown = reference_dilate(strong, w, h, distance) if distance > 0 else strong
    stray = bytes(1 if 0 < v < below and not grown[i] else 0 for i, v in enumerate(alpha))
    want = a.components(stray, w, h, 3)
    assert specks["count"] == want["specks"] and specks["pixels"] == want["speck_pixels"]
    assert specks["regions"] == want["regions"] and specks["truncated"] is False
    assert specks["samples"] == [rect(s["x"] + 100, s["y"] + 200, s["w"], s["h"]) for s in want["samples"]]


def test_stray_stats_glow_is_a_region_not_a_speck():
    # A faint 12x12 glow far from the strong block, and a 2-pixel speck.
    w, h = 40, 20
    alpha = bytearray(w * h)
    for y in range(4, 8):
        alpha[y * w + 2:y * w + 6] = b"\xff" * 4
    for y in range(4, 16):
        alpha[y * w + 20:y * w + 32] = b"\x03" * 12
    alpha[18 * w + 2] = alpha[18 * w + 3] = 5
    count, specks, dilated = a.stray_stats(bytes(alpha), w, h, 8, 4, 64)
    assert dilated and count == 146
    assert specks == {"count": 1, "pixels": 2, "regions": 1, "samples": [rect(2, 18, 2, 1)],
                      "bounds": rect(2, 18, 2, 1), "truncated": False}


def test_stray_stats_without_stray_pixels():
    assert a.stray_stats(bytes([0, 255, 255, 0]), 4, 1, 8, 4, 64) == (0, a.empty_specks(), False)
    assert a.stray_stats(bytes([3, 255, 0, 0]), 4, 1, 8, 1, 64) == (0, a.empty_specks(), True)
    # No strong pixel: every weak pixel is stray, without a dilation.
    count, specks, dilated = a.stray_stats(bytes([3, 0, 3, 3]), 4, 1, 8, 4, 64)
    assert (count, specks["count"], specks["pixels"], dilated) == (3, 2, 3, False)


def test_empty_specks_is_a_fresh_dict():
    a.empty_specks()["samples"].append(1)
    assert a.empty_specks() == {"count": 0, "pixels": 0, "regions": 0, "samples": [],
                                "bounds": None, "truncated": False}


# --- layer_stats / trim_layers helpers (P5.3) ---------------------------

def test_check_buckets():
    assert a.check_buckets(None) == [0, 1, 8, 32, 128, 255]
    assert a.check_buckets([0, 255]) == [0, 255]
    assert a.check_buckets(tuple(range(16))) == list(range(16))
    for bad in ([0], list(range(17)), [0, 0], [5, 3], [0, 256], [-1, 4], [0, 1.5], [0, True],
                "0,1", {"a": 1}):
        with pytest.raises(ValueError):
            a.check_buckets(bad)


def test_histogram_matches_a_reference_count():
    rng = random.Random(7)
    alpha = bytes(rng.randrange(256) for _ in range(5000)) + bytes([0, 1, 7, 8, 254, 255] * 3)
    for edges in ([0, 1, 8, 32, 128, 255], [0, 255], [10, 20, 200], list(range(0, 256, 17))):
        got = a.histogram(alpha, edges)
        uppers = edges[1:] + [256]
        want = [sum(1 for v in alpha if lo <= v < hi) for lo, hi in zip(edges, uppers)]
        assert got == {"edges": edges, "counts": want}, edges
    assert a.histogram(bytes([0, 0, 3, 255]), [0, 1, 8, 32, 128, 255])["counts"] == \
        [2, 1, 0, 0, 0, 1]
    assert sum(a.histogram(bytes([1, 2, 9]), [5, 10])["counts"]) == 1  # below edges[0]: none


def test_beyond_canvas():
    assert a.beyond_canvas(None, 10, 10) is None
    assert a.beyond_canvas(rect(0, 0, 10, 10), 10, 10) is None
    assert a.beyond_canvas(rect(-2, 3, 4, 9), 10, 10) == {"left": 2, "top": 0, "right": 0,
                                                          "bottom": 2}
    assert a.beyond_canvas(rect(8, -1, 5, 1), 10, 10) == {"left": 0, "top": 1, "right": 3,
                                                          "bottom": 0}


def test_count_inside():
    # a 4x3 mask of ones at (-1, 8) on a 10x10 canvas: columns 0..2, rows 8..9
    ones = bytes([255]) * 12
    assert a.count_inside(ones, 4, 3, (-1, 8), 10, 10) == 6
    assert a.count_inside(ones, 4, 3, (0, 0), 10, 10) == 12
    assert a.count_inside(ones, 4, 3, (20, 0), 10, 10) == 0
    mixed = bytes([0, 5, 0, 5, 5, 0, 0, 0, 5, 5, 5, 5])
    assert a.count_inside(mixed, 4, 3, (-2, -1), 10, 10) == 2  # columns 2-3 of rows 1-2


def test_trim_mask_threshold_and_near():
    alpha_rows = ["9999......",
                  "93........",
                  "..........",
                  ".......2..",
                  ".........1"]
    alpha = bytes(int(c) * 20 if c != "." else 0 for row in alpha_rows for c in row)
    w, h = 10, 5
    # threshold 100: values 20..80 are faint (cells 1-4), 9 -> 180 strong
    mask, count = a.trim_mask(alpha, w, h, 100)
    assert count == 3 and mask.count(1) == 3
    mask, count = a.trim_mask(alpha, w, h, 100, near_px=2)
    assert count == 2  # the "3" next to the strong block is kept
    assert mask[1 * w + 1] == 0 and mask[3 * w + 7] == 1 and mask[4 * w + 9] == 1
    assert a.trim_mask(bytes(4), 2, 2, 8) == (bytes(4), 0)
    # no strong pixel: every faint one goes, near_px or not
    assert a.trim_mask(bytes([0, 3, 5, 0]), 2, 2, 8, near_px=4)[1] == 2


def test_zero_pixels():
    w, h = 3, 3
    data = bytes(range(1, 37))
    mask = bytes([0, 1, 0,
                  1, 1, 1,
                  0, 0, 0])
    out = a.zero_pixels(data, mask, w)
    want = bytearray(data)
    for i, m in enumerate(mask):
        if m:
            want[4 * i:4 * i + 4] = bytes(4)
    assert out == bytes(want)
    assert a.zero_pixels(data, bytes(9), w) == data
    rng = random.Random(3)
    w, h = 37, 11
    data = bytes(rng.randrange(256) for _ in range(4 * w * h))
    mask = bytes(rng.random() < 0.3 for _ in range(w * h))
    want = bytearray(data)
    for i, m in enumerate(mask):
        if m:
            want[4 * i:4 * i + 4] = bytes(4)
    assert a.zero_pixels(data, mask, w) == bytes(want)
