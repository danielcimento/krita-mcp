"""kritamcp/changes.py: the painting tools' changed-pixel mask and notes
(F005 K11)."""

import os

import pytest

from kritamcp import changes


def px(*alphas, bgr=(1, 2, 3)):
    return b"".join(bytes((*bgr, a)) for a in alphas)


def test_changed_mask_any_channel():
    old = px(0, 0, 0, 0)
    new = bytearray(old)
    new[0] ^= 1          # pixel 0, blue
    new[4 * 2 + 3] = 9   # pixel 2, alpha
    assert changes.changed_mask(old, bytes(new)) == b"\1\0\1\0"
    assert changes.changed_mask(old, old) == bytes(4)
    assert changes.changed_mask(b"", b"") == b""


def test_changed_mask_matches_a_plain_loop_across_strips(monkeypatch):
    monkeypatch.setattr(changes, "STRIP_BYTES", 4 * 7)  # several strips, one partial
    old = os.urandom(4 * 50)
    new = bytearray(old)
    for i in (0, 6, 7, 13, 49):
        new[4 * i + i % 4] ^= 0x80
    want = bytes(int(old[4 * i:4 * i + 4] != new[4 * i:4 * i + 4]) for i in range(50))
    assert changes.changed_mask(old, bytes(new)) == want


def test_changed_mask_needs_equal_pixel_buffers():
    with pytest.raises(ValueError):
        changes.changed_mask(bytes(8), bytes(4))
    with pytest.raises(ValueError):
        changes.changed_mask(bytes(6), bytes(6))


def test_mask_bounds_crop_and_counts():
    w, h = 5, 4
    mask = bytearray(w * h)
    mask[1 * w + 3] = mask[2 * w + 1] = 1
    mask = bytes(mask)
    assert changes.mask_bounds(mask, w, h) == (1, 1, 3, 2)
    assert changes.mask_bounds(bytes(w * h), w, h) is None
    assert changes.crop(mask, w, (1, 1, 3, 2)) == b"\0\0\1\1\0\0"
    assert changes.and_count(mask, changes.at_least(bytes(range(20)), 8)) == 2  # 8 and 11
    assert changes.and_count(mask, changes.at_least(bytes(range(20)), 9)) == 1
    assert changes.and_count(b"", b"") == 0


def test_intersect():
    assert changes.intersect((0, 0, 10, 10), (5, 5, 10, 10)) == (5, 5, 5, 5)
    assert changes.intersect((0, 0, 10, 10), (10, 0, 5, 5)) is None


def report(**given):
    out = {"changed_px": 5, "layer_visible": True, "hidden_by": None, "covered_pct": 0.2,
           "fully_hidden": False, "top_cover": None}
    out.update(given)
    return out


def test_notes():
    assert changes.notes(report(), "T") == []
    assert changes.notes(report(changed_px=0), "T")[0]["code"] == "nothing_changed"
    (n,) = changes.notes(report(layer_visible=False, hidden_by="T", covered_pct=None), "T")
    assert n == {"code": "layer_hidden",
                 "message": "this change isn't visible in the composite: the layer is hidden"}
    (n,) = changes.notes(report(layer_visible=False, hidden_by="Arm B", covered_pct=None), "Arm B/T")
    assert n["message"].endswith("the layer's group 'Arm B' is hidden")
    (n,) = changes.notes(report(covered_pct=1.0, fully_hidden=True,
                                top_cover={"id": "{1}", "path": "Head/Neck"}), "T")
    assert n == {"code": "change_covered",
                 "message": "this change isn't visible in the composite: 100% covered by layers "
                            "above (top cover: 'Head/Neck')"}
    (n,) = changes.notes(report(covered_pct=1.0), "T")
    assert "hardly shows" in n["message"] and "top cover" not in n["message"]
    assert changes.notes(report(covered_pct=0.99), "T") == []
