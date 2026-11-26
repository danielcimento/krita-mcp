"""canvasplan (set_canvas_size offsets) and diffplan (diff_documents
matching and pixel comparison): pure, no Krita."""

import random

import pytest

from kritamcp import canvasplan, diffplan
from kritamcp.errors import CommandError


# --- canvasplan ------------------------------------------------------------------

@pytest.mark.parametrize("anchor, grow, shrink", [
    ("top-left", (0, 0), (0, 0)), ("top", (4, 0), (-4, 0)), ("top-right", (9, 0), (-7, 0)),
    ("left", (0, 3), (0, -3)), ("center", (4, 3), (-4, -3)), ("right", (9, 3), (-7, -3)),
    ("bottom-left", (0, 7), (0, -5)), ("bottom", (4, 7), (-4, -5)),
    ("bottom-right", (9, 7), (-7, -5)),
])
def test_offsets(anchor, grow, shrink):
    assert canvasplan.offset(anchor, (32, 24), (41, 31)) == grow
    assert canvasplan.offset(anchor, (32, 24), (25, 19)) == shrink
    assert canvasplan.offset(anchor, (32, 24), (32, 24)) == (0, 0)


def test_center_rounds_toward_the_top_left():
    # Odd differences: the art sits half a pixel left of / above centre.
    assert canvasplan.offset("center", (10, 10), (13, 13)) == (1, 1)  # 1 left, 2 right
    assert canvasplan.offset("center", (13, 13), (10, 10)) == (-2, -2)  # 2 off the left, 1 right
    assert canvasplan.krita_rect((1, -2), (13, 10)) == (-1, 2, 13, 10)


def test_check_anchor():
    assert canvasplan.check_anchor("center") == "center"
    for bad in ("middle", "Center", None, 1):
        with pytest.raises(CommandError) as info:
            canvasplan.check_anchor(bad)
        assert info.value.code == "bad_request"
        assert info.value.details["param"] == "anchor"
        assert info.value.details["valid"] == list(canvasplan.ANCHORS)


# --- diffplan: matching ------------------------------------------------------------

def rec(nid, name, path, parent=None, type="paintlayer", **props):
    base = {"id": nid, "name": name, "path": path, "parent": parent, "type": type,
            "visible": True, "opacity": 1.0, "blend_mode": "normal", "locked": False,
            "alpha_locked": False, "inherit_alpha": False,
            "pass_through": False if type == "grouplayer" else None,
            "has_layer_style": False, "bounds": (0, 0, 1, 1)}
    base.update(props)
    return base


def tree(*records):
    return diffplan.with_indices(list(records))


def test_with_indices_counts_from_the_bottom_per_parent():
    out = tree(rec("t", "T", "T"), rec("g", "G", "G", type="grouplayer"),
               rec("a", "A", "G/A", "g"), rec("b", "B", "G/B", "g"), rec("z", "Z", "Z"))
    assert {r["id"]: r["index"] for r in out} == {"t": 2, "g": 1, "a": 1, "b": 0, "z": 0}


def test_match_passes_in_order():
    a = tree(rec("1", "Same", "Same"), rec("2", "P", "P"), rec("3", "Uniq", "G/Uniq"),
             rec("4", "Dup", "X/Dup"), rec("5", "Dup", "Y/Dup"), rec("6", "Gone", "Gone"))
    b = tree(rec("1", "Renamed", "Renamed"), rec("9", "P", "P"), rec("8", "Uniq", "Top/Uniq"),
             rec("7", "Dup", "Z/Dup"), rec("6b", "Dup", "Y/Dup"), rec("new", "New", "New"))
    assert diffplan.match(a, b) == [("1", "1", "uuid"), ("2", "9", "path"),
                                    ("3", "8", "name"), ("5", "6b", "path")]


def test_repeated_paths_and_names_are_not_matched():
    a = tree(rec("1", "L", "L"), rec("2", "L", "L"))
    b = tree(rec("3", "L", "L"), rec("4", "L", "L"))
    assert diffplan.match(a, b) == []
    # Unique among the unmatched is not enough for a name: the whole doc counts.
    a = tree(rec("1", "L", "A/L"), rec("2", "L", "B/L"))
    b = tree(rec("2", "L", "B/L"), rec("3", "L", "C/L"))
    assert diffplan.match(a, b) == [("2", "2", "uuid")]


def test_uuid_match_wins_over_a_path_elsewhere():
    a = tree(rec("1", "A", "A"), rec("2", "B", "B"))
    b = tree(rec("2", "A", "A"), rec("3", "B", "B"))
    # "2" matches by id; then "A" (a: 1) and "B" (b: 3) have no path partner left.
    assert diffplan.match(a, b) == [("2", "2", "uuid")]


def test_diff_categories():
    a = tree(rec("g", "G", "G", type="grouplayer"), rec("x", "X", "G/X", "g"),
             rec("y", "Y", "G/Y", "g"), rec("r", "Old", "Old"), rec("del", "Del", "Del"))
    b = tree(rec("x", "X", "X"), rec("n", "N", "N"), rec("g", "G", "G", type="grouplayer"),
             rec("y", "Y", "G/Y", "g", opacity=0.5, visible=False),
             rec("r", "New", "New", type="vectorlayer"))
    out = diffplan.diff(a, b)
    assert out["added"] == [{"id": "n", "name": "N", "path": "N", "type": "paintlayer"}]
    assert out["removed"] == [{"id": "del", "name": "Del", "path": "Del", "type": "paintlayer"}]
    assert out["renamed"] == [{"id_a": "r", "id_b": "r", "path": "New", "path_a": "Old",
                               "match": "uuid", "name": ["Old", "New"]}]
    moved = {m["id_b"]: m for m in out["moved"]}
    assert moved["x"] == {"id_a": "x", "id_b": "x", "path": "X", "path_a": "G/X",
                          "match": "uuid", "parent": ["g", None], "index": [1, 3],
                          "parent_changed": True}
    # G: index 2 -> 1 at the top level; Y: index 0 -> 0 in G, so not moved.
    assert moved["g"]["parent_changed"] is False and moved["g"]["index"] == [2, 1]
    assert "y" not in moved
    changed = {c["id_b"]: c["fields"] for c in out["changed"]}
    assert changed == {"y": {"visible": [True, False], "opacity": [1.0, 0.5]},
                       "r": {"type": ["paintlayer", "vectorlayer"]}}
    assert out["matched"] == {"uuid": 4, "path": 0, "name": 0, "total": 4}
    # Only paint layers in both are compared for pixels.
    assert [p[1]["id"] for p in diffplan.pixel_pairs(out["pairs"])] == ["x", "y"]


def test_parent_matched_by_identity_not_by_id():
    # The group was rebuilt (new id, same path): its children stay "in place".
    a = tree(rec("g1", "G", "G", type="grouplayer"), rec("c1", "C", "G/C", "g1"))
    b = tree(rec("g2", "G", "G", type="grouplayer"), rec("c2", "C", "G/C", "g2"))
    out = diffplan.diff(a, b)
    assert out["moved"] == [] and out["matched"]["path"] == 2


def test_empty_layers_are_not_pixel_compared():
    a = tree(rec("1", "E", "E", bounds=None))
    b = tree(rec("1", "E", "E", bounds=None))
    assert diffplan.pixel_pairs(diffplan.diff(a, b)["pairs"]) == []


# --- diffplan: pixels --------------------------------------------------------------

def brute(da, db, rect):
    """Per-pixel reference: changed unless equal, or alpha 0 in both."""
    x, y, w, h = rect
    count, xs, ys = 0, [], []
    for j in range(h):
        for i in range(w):
            o = 4 * (j * w + i)
            pa, pb = da[o:o + 4], db[o:o + 4]
            if pa != pb and not (pa[3] == 0 and pb[3] == 0):
                count += 1
                xs.append(x + i)
                ys.append(y + j)
    if not count:
        return 0, None
    return count, (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def reader(data, rect):
    x0, y0, w, _ = rect

    def read(x, y, rw, rh):
        assert x == x0 and rw == w
        o = 4 * w * (y - y0)
        return data[o:o + 4 * w * rh]
    return read


@pytest.mark.parametrize("seed", range(8))
def test_diff_region_matches_brute_force(seed):
    rnd = random.Random(seed)
    w, h = rnd.randint(1, 23), rnd.randint(1, 17)
    rect = (rnd.randint(-5, 5), rnd.randint(-5, 5), w, h)
    da = bytearray(rnd.randrange(256) for _ in range(4 * w * h))
    for i in range(3, len(da), 4):  # plenty of alpha 0
        if rnd.random() < 0.4:
            da[i] = 0
    db = bytearray(da)
    for _ in range(rnd.randint(0, 12)):
        o = 4 * rnd.randrange(w * h)
        c = rnd.randrange(4)
        db[o + c] = rnd.randrange(256)
    da, db = bytes(da), bytes(db)
    for strip in (1, w, 3 * w, 10 ** 6):
        assert diffplan.diff_region(reader(da, rect), reader(db, rect), rect,
                                    strip_pixels=strip) == brute(da, db, rect)


def test_diff_row_details():
    w = 5
    a = bytes(4 * w)
    b = bytearray(a)
    b[4 * 1:4 * 1 + 3] = b"\x01\x02\x03"  # colour under alpha 0 in both: equal
    assert diffplan.diff_row(a, bytes(b), w) is None
    b[4 * 3 + 3] = 1  # alpha 0 -> 1
    b[4 * 4 + 0] = 9  # alpha 0 in both
    assert diffplan.diff_row(a, bytes(b), w) == (1, 3, 3)
    c = bytes((7, 7, 7, 255)) * w
    d = bytearray(c)
    d[0] = 8
    d[4 * 4 + 2] = 0
    assert diffplan.diff_row(c, bytes(d), w) == (2, 0, 4)
    assert diffplan.diff_row(c, c, w) is None


def test_union():
    assert diffplan.union(None, None) is None
    assert diffplan.union((1, 2, 3, 4), None) == (1, 2, 3, 4)
    assert diffplan.union((0, 0, 2, 2), (5, -1, 1, 1)) == (0, -1, 6, 3)
