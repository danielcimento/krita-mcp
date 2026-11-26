"""Pixel I/O between PNG files and Krita layers (D5): QImage, no numpy.

Krita's 8-bit RGBA ``pixelData`` / ``setPixelData`` bytes are B, G, R, A
per pixel, non-premultiplied, which is ``QImage.Format_ARGB32``'s layout on
little-endian machines. PNG load and save through QImage is bit-exact,
colors under zero alpha included (spike Q5).

Buffers here are "BGRA bytes": ``4 * w * h`` bytes, rows top to bottom with
no padding. Functions accept ``bytes``, ``bytearray``, ``memoryview`` or a
``QByteArray`` (what ``pixelData`` returns) and return ``bytes``.
Rectangles are ``(x, y, w, h)`` tuples in buffer coordinates.

NO PER-PIXEL PYTHON LOOPS. Whole-buffer work uses C-level ``bytes``
operations (slicing, ``translate``, ``count``, ``lstrip``), ``int`` bitwise
operations over whole rows, or ``QPainter``. Loops over rows are fine.
"""

import os
import struct
import sys
import uuid
import zlib

from PyQt5.QtCore import QByteArray
from PyQt5.QtGui import QImage, QPainter

from . import config
from .errors import CommandError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HEADER_LEN = 8 + 4 + 4 + 13 + 4  # signature, IHDR length, type, data, CRC

RGBA8 = ("RGBA", "U8")

# 256-entry translate tables: 0xFF where the byte is zero / nonzero, else 0.
_ZERO_FF = bytes([0xFF] + [0] * 255)


def _at_least(threshold):
    """Translate table: 1 for bytes >= ``threshold``, else 0."""
    return bytes(1 if v >= threshold else 0 for v in range(256))


def _check_threshold(threshold):
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 255:
        raise ValueError(f"threshold must be an int in 1..255, got {threshold!r}")


def _require_little_endian():
    if sys.byteorder != "little":  # ARGB32 is BGRA in memory only on little-endian
        raise CommandError("krita_error", "Pixel I/O needs a little-endian machine")


def as_bytes(data):
    """``data`` (bytes-like or ``QByteArray``) as ``bytes``; no copy for bytes."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, QByteArray):
        return data.data()
    return bytes(data)


def _buffer(data, w, h):
    data = as_bytes(data)
    if isinstance(w, bool) or isinstance(h, bool) or not (isinstance(w, int) and isinstance(h, int)):
        raise ValueError(f"width and height must be ints, got {w!r} x {h!r}")
    if w < 1 or h < 1:
        raise ValueError(f"bad buffer size {w}x{h}")
    if len(data) != 4 * w * h:
        raise ValueError(f"buffer is {len(data)} bytes; {w}x{h} BGRA needs {4 * w * h}")
    return data


# --- QImage ----------------------------------------------------------------

def wrap(data, w, h):
    """A ``Format_ARGB32`` QImage over ``data`` (BGRA bytes), not a copy.

    THE BYTES MUST OUTLIVE THE IMAGE: Qt reads the caller's memory, so keep
    a reference to ``data`` for as long as the image (or a painter on it)
    is used, or call ``to_qimage`` instead. Treat the image as read-only.
    """
    _require_little_endian()
    data = _buffer(data, w, h)
    return QImage(data, w, h, 4 * w, QImage.Format_ARGB32)


def to_qimage(data, w, h):
    """A ``Format_ARGB32`` QImage holding its own copy of ``data``."""
    data = as_bytes(data)
    return wrap(data, w, h).copy()


def from_qimage(image):
    """``(bytes, w, h)``: the image's pixels as BGRA bytes (converted to
    ``Format_ARGB32`` first if needed; any row padding is dropped)."""
    _require_little_endian()
    if image.isNull():
        raise CommandError("krita_error", "Cannot read pixels from a null image")
    if image.format() != QImage.Format_ARGB32:
        image = image.convertToFormat(QImage.Format_ARGB32)
    w, h, stride = image.width(), image.height(), image.bytesPerLine()
    raw = image.constBits().asstring(image.sizeInBytes())
    row = 4 * w
    if stride == row:
        data = raw[:row * h]
    else:
        data = b"".join(raw[y * stride:y * stride + row] for y in range(h))
    if len(data) != row * h:
        raise CommandError("krita_error", f"Read {len(data)} bytes for a {w}x{h} image")
    return data, w, h


# --- PNG -------------------------------------------------------------------

def png_size(path):
    """``(width, height)`` from the PNG's IHDR chunk, reading only the
    first 33 bytes (cheap enough to validate a batch of PNGs up front).

    The signature, the IHDR chunk's length, type and CRC are checked; the
    image data is not, so a file truncated after its header passes here and
    fails in ``load_png_bgra``. Errors: ``bad_request`` (not a PNG, or a
    corrupt header), ``not_found``.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_HEADER_LEN)
    except FileNotFoundError:
        raise CommandError("not_found", f"File not found: {path}") from None
    except OSError as e:
        raise CommandError("bad_request", f"Cannot read {path}: {e.strerror or e}") from None
    if len(head) < _HEADER_LEN or head[:8] != PNG_SIGNATURE:
        raise CommandError("bad_request", f"Not a PNG file: {path}", {"path": path})
    length, ctype = struct.unpack(">I4s", head[8:16])
    crc = struct.unpack(">I", head[29:33])[0]
    if length != 13 or ctype != b"IHDR" or zlib.crc32(head[12:29]) != crc:
        raise CommandError("bad_request", f"Corrupt PNG header: {path}", {"path": path})
    width, height = struct.unpack(">II", head[16:24])
    if width == 0 or height == 0 or width > 2**31 - 1 or height > 2**31 - 1:
        raise CommandError("bad_request", f"Bad PNG size {width}x{height}: {path}", {"path": path})
    return width, height


def check_png_limits(size, given):
    """Raise ``too_large`` if a ``(w, h)`` PNG is over the canvas limits
    (``config.current().limits``); ``given`` is the path as the caller
    wrote it, for the message."""
    limits = config.current().limits
    w, h = size
    max_dim, max_pixels = limits["max_canvas_dim"], limits["max_canvas_pixels"]
    if max(w, h) > max_dim:
        raise CommandError("too_large",
                           f"PNG {given} is {w}x{h}; the limit is {max_dim} pixels per side "
                           "(max_canvas_dim)",
                           {"limit": "max_canvas_dim", "max": max_dim, "w": w, "h": h})
    if w * h > max_pixels:
        raise CommandError("too_large",
                           f"PNG {given} is {w}x{h} = {w * h} pixels; the limit is {max_pixels} "
                           "(max_canvas_pixels)",
                           {"limit": "max_canvas_pixels", "max": max_pixels, "pixels": w * h})


def load_png_bgra(path):
    """Decode a PNG into Krita's 8-bit BGRA bytes: ``(data, width, height)``.

    ``data`` is a ``bytes`` copy, ``4 * width * height`` long, rows top to
    bottom with no padding. Errors: ``bad_request`` if Qt cannot decode
    the file as a PNG.
    """
    _require_little_endian()
    image = QImage(path, "PNG")
    if image.isNull():
        raise CommandError("bad_request", f"Cannot decode PNG: {path}", {"path": path})
    return from_qimage(image)


def png_quality(level):
    """The ``QImage.save`` quality that makes Qt 5 write zlib level ``level``
    (0-9); ``None`` gives -1, Qt's default (no level set: libpng's default,
    zlib's ``Z_DEFAULT_COMPRESSION``, i.e. 6).

    Qt 5.15 ``src/gui/image/qpnghandler.cpp``, ``write_png_image``:
    ``QImage::save`` sets the handler's quality and leaves its compression
    at -1 (``QImageWriter``'s default), so ``compression = 100 -
    qMin(quality, 100)`` and then ``compression = (compression * 9) / 91``
    ("map [0,100] -> [0,9]"), passed to ``png_set_compression_level``; a
    quality of -1 skips that call. The inverse used here is the largest
    quality giving ``level``: ``100 - ceil(91 * level / 9)``.
    """
    if level is None:
        return -1
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 9:
        raise ValueError(f"PNG level must be an int in 0..9 or None, got {level!r}")
    return 100 - (91 * level + 8) // 9


def save_png(image_or_data, path, w=None, h=None, level=None):
    """Write a PNG atomically; returns its size in bytes.

    ``image_or_data``: a QImage, or BGRA bytes with ``w`` and ``h``.
    ``level``: zlib level 0 (fastest, biggest) to 9 (smallest), or None
    for Qt's default. The image goes to a hidden temp file in the same
    directory, which then replaces ``path`` (``os.replace``), so a failed
    write never leaves a partial file at ``path``. ``path`` must already be
    checked against the allowed roots and its directory must exist.
    Errors: ``krita_error`` (Qt could not write, or the rename failed).
    """
    if isinstance(image_or_data, QImage):
        image = image_or_data
        if image.isNull():
            raise CommandError("krita_error", f"Cannot save a null image to {path}")
    else:
        data = as_bytes(image_or_data)  # kept referenced while ``image`` wraps it
        image = wrap(data, w, h)
    quality = png_quality(level)
    directory, base = os.path.split(os.path.abspath(path))
    tmp = os.path.join(directory, f".{base}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        if not image.save(tmp, "PNG", quality):
            raise CommandError("krita_error", f"Qt could not write the PNG {path}", {"path": path})
        try:
            os.replace(tmp, path)
        except OSError as e:
            raise CommandError("krita_error", f"Cannot write {path}: {e.strerror or e}",
                               {"path": path}) from e
    finally:
        if os.path.lexists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return os.path.getsize(path)


# --- Whole-buffer operations ----------------------------------------------

def alpha(data):
    """The alpha bytes of BGRA ``data``, one per pixel."""
    return as_bytes(data)[3::4]


def count_alpha(data, threshold=1):
    """The number of pixels with alpha >= ``threshold`` (1-255)."""
    _check_threshold(threshold)
    a = alpha(data)
    if threshold == 1:
        return len(a) - a.count(0)
    return a.translate(_at_least(threshold)).count(1)


def opaque_bounds(data, w, h, threshold=1):
    """The tight ``(x, y, w, h)`` around the pixels with alpha >=
    ``threshold`` (1-255), or None if there are none.

    Row pass: the first and last nonzero bytes of the alpha mask
    (``lstrip`` / ``rstrip`` of zero bytes, in C) give the top and bottom
    rows. Column pass: the mask rows between them are OR-ed together as
    big ints (one ``int.from_bytes`` per row: O(rows) Python steps, each
    O(width) in C), and the same strip on the result gives the left and
    right columns. Bitwise OR has no carries, so each byte of the result is
    nonzero exactly when its column has a pixel at or above the threshold.
    """
    _check_threshold(threshold)
    data = _buffer(data, w, h)
    mask = data[3::4]
    if threshold > 1:
        mask = mask.translate(_at_least(threshold))
    n = len(mask)
    lead = n - len(mask.lstrip(b"\0"))
    if lead == n:
        return None
    last = len(mask.rstrip(b"\0")) - 1
    y0, y1 = lead // w, last // w
    view = memoryview(mask)
    acc = 0
    for y in range(y0, y1 + 1):
        acc |= int.from_bytes(view[y * w:(y + 1) * w], "big")
    cols = acc.to_bytes(w, "big")
    x0 = w - len(cols.lstrip(b"\0"))
    x1 = len(cols.rstrip(b"\0")) - 1
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def crop(data, w, h, rect):
    """The BGRA bytes of ``rect`` (``(x, y, w, h)``, inside the buffer)."""
    data = _buffer(data, w, h)
    x, y, cw, ch = rect
    if cw < 1 or ch < 1 or x < 0 or y < 0 or x + cw > w or y + ch > h:
        raise ValueError(f"crop rect {rect!r} is not inside the {w}x{h} buffer")
    stride = 4 * w
    if x == 0 and cw == w:
        return data[y * stride:(y + ch) * stride]
    start, row = 4 * x, 4 * cw
    return b"".join(data[r * stride + start:r * stride + start + row] for r in range(y, y + ch))


def _expand4(mask_row):
    """Each byte of ``mask_row`` repeated 4 times (a pixel mask to a byte mask)."""
    out = bytearray(4 * len(mask_row))
    for c in range(4):
        out[c::4] = mask_row
    return out


def _select(out, mask, other, w, h):
    """In place: ``out`` (a bytearray of BGRA) takes ``other``'s pixel
    wherever ``mask`` (one byte per pixel, 0x00 or 0xFF) is 0xFF. One pass
    over rows; mixed rows are merged as big ints, ``(o & ~m) | (x & m)``."""
    stride = 4 * w
    for y in range(h):
        p, b = y * w, y * stride
        hits = mask.count(0xFF, p, p + w)
        if hits == 0:
            continue
        if hits == w:
            out[b:b + stride] = other[b:b + stride]
            continue
        m = int.from_bytes(_expand4(mask[p:p + w]), "little")
        o = int.from_bytes(out[b:b + stride], "little")
        x = int.from_bytes(other[b:b + stride], "little")
        out[b:b + stride] = ((o & ~m) | (x & m)).to_bytes(stride, "little")


# Rows per QPainter pass in ``composite_over``: about 4 MP, so the 16-bit
# working image stays near 32 MB whatever the size of the rectangle.
_STRIP_PIXELS = 4 * 1024 * 1024


def _qt_over(dst, src, w, h):
    """QPainter source-over of ``src`` onto ``dst`` (BGRA bytes, same size)
    on a ``Format_RGBA64_Premultiplied`` working image; BGRA bytes back."""
    work = wrap(dst, w, h).convertToFormat(QImage.Format_RGBA64_Premultiplied)
    source = wrap(src, w, h)
    painter = QPainter(work)
    try:
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.drawImage(0, 0, source)
    finally:
        painter.end()
    return from_qimage(work)[0]


def composite_over(dst, src, w, h):
    """Source-over of ``src`` onto ``dst``, both non-premultiplied BGRA of
    the same size; returns the result as BGRA bytes.

    ``QPainter`` with ``CompositionMode_SourceOver`` draws ``src`` onto a
    premultiplied copy of ``dst``, in strips of rows, converted back to
    ``Format_ARGB32``. The working image is ``Format_RGBA64_Premultiplied``
    (16 bits per channel), not ``Format_ARGB32_Premultiplied``: 8-bit
    premultiplication loses colour precision as alpha drops, and with Qt
    5.15 it was off from the exact formula by up to 3.3 levels at result
    alpha 128-254 and 51 at alpha 1-7. At 16 bits every channel came out
    within 1 level of the exact value (under 0.9) at every alpha
    (tests/krita/test_pixels.py measures it). Three cases are made exact
    afterwards, with no per-pixel loop (row masks, ``_select``):

    - src alpha 255: the src pixel (Qt already copies it);
    - dst alpha 0 (and src alpha > 0): the src pixel, bit for bit;
    - src alpha 0: the dst pixel, bit for bit, colour under zero alpha
      included, so a PNG's transparent parts leave the layer untouched.
    """
    _require_little_endian()
    dst = _buffer(dst, w, h)
    src = _buffer(src, w, h)
    rows = max(1, _STRIP_PIXELS // w)
    stride = 4 * w
    out = bytearray()
    for y in range(0, h, rows):
        n = min(rows, h - y)
        part = slice(y * stride, (y + n) * stride)
        out += _qt_over(dst[part], src[part], w, n)
    if len(out) != len(dst):
        raise CommandError("krita_error", f"Compositing gave {len(out)} bytes for {w}x{h}")
    src_alpha, dst_alpha = src[3::4], dst[3::4]
    _select(out, dst_alpha.translate(_ZERO_FF), src, w, h)   # dst transparent: src
    _select(out, src_alpha.translate(_ZERO_FF), dst, w, h)   # src transparent: dst
    return bytes(out)


def _alpha8(data, w, h):
    """A ``Format_Alpha8`` QImage over ``data`` (``w * h`` bytes, no row
    padding: the explicit ``bytesPerLine`` lets rows be any length). Not a
    copy: the bytes must outlive the image."""
    return QImage(data, w, h, w, QImage.Format_Alpha8)


def alpha8_bytes(image):
    """The bytes of a ``Format_Alpha8`` image, one per pixel, without Qt's
    row padding (Qt pads rows to 4 bytes)."""
    w, h, stride = image.width(), image.height(), image.bytesPerLine()
    raw = image.constBits().asstring(image.sizeInBytes())
    if stride == w:
        return raw[:w * h]
    return b"".join(raw[y * stride:y * stride + w] for y in range(h))


def _scaled_alpha(alpha_bytes, mask, w, h, mode):
    """``alpha_bytes`` scaled by ``mask`` (both ``w * h`` bytes) with QPainter
    ``mode`` (DestinationIn: ``a * s / 255``; DestinationOut: ``a * (255 -
    s) / 255``), on a 16-bit premultiplied working image."""
    work = _alpha8(alpha_bytes, w, h).convertToFormat(QImage.Format_RGBA64_Premultiplied)
    source = _alpha8(mask, w, h)
    painter = QPainter(work)
    try:
        painter.setCompositionMode(mode)
        painter.drawImage(0, 0, source)
    finally:
        painter.end()
    return alpha8_bytes(work.convertToFormat(QImage.Format_Alpha8))


def mask_alpha(data, mask, w, h, keep=True):
    """BGRA ``data`` with each pixel's alpha scaled by the selection value
    in ``mask`` (one byte per pixel, ``w * h``): ``keep`` gives
    ``round(alpha * s / 255)`` (the part inside the selection), otherwise
    ``round(alpha * (255 - s) / 255)`` (what a cut leaves). Colours are
    unchanged (non-premultiplied). Returns ``(bytes, new_alpha)``.

    Only the alpha bytes go through ``QPainter`` (``CompositionMode_DestinationIn``
    / ``DestinationOut`` with the mask as a ``Format_Alpha8`` image), on a
    ``Format_RGBA64_Premultiplied`` working image in strips of rows. That is
    exact: all 65536 (alpha, s) pairs give the rounded product (Qt 5.15.7,
    tests/krita/test_selection.py); an 8-bit working image (``Format_Alpha8``
    or ``Format_ARGB32_Premultiplied``) is off by one for 24 of them. The
    colour bytes never pass through a premultiplied image, so they stay bit
    for bit. ``s = 255`` keeps the alpha (``keep``) or clears it, ``s = 0``
    the reverse. Zeroing the colour under a cleared alpha is the caller's
    choice (``alphastats.zero_pixels``)."""
    _require_little_endian()
    data = _buffer(data, w, h)
    new_alpha = scale_alpha(data[3::4], mask, w, h, keep=keep)
    out = bytearray(data)
    out[3::4] = new_alpha
    return bytes(out), new_alpha


def scale_alpha(alpha_bytes, mask, w, h, keep=True):
    """``round(a * s / 255)`` (``keep``) or ``round(a * (255 - s) / 255)``
    for each pair of bytes of ``alpha_bytes`` and ``mask`` (both ``w * h``,
    one byte per pixel); returns ``w * h`` bytes. The products never land on
    a half (255 is odd), so there is no tie to round.

    ``mask_alpha``'s alpha path: ``QPainter`` ``DestinationIn`` /
    ``DestinationOut`` on a ``Format_RGBA64_Premultiplied`` working image,
    in strips of rows, exact for all 65536 pairs (Qt 5.15.7). Also the
    product of two alpha planes (``occlusion_map``: target alpha x
    coverage)."""
    alpha_bytes, mask = as_bytes(alpha_bytes), as_bytes(mask)
    if len(alpha_bytes) != w * h or len(mask) != w * h:
        raise ValueError(f"alpha is {len(alpha_bytes)} and mask {len(mask)} bytes; "
                         f"{w}x{h} needs {w * h}")
    mode = (QPainter.CompositionMode_DestinationIn if keep
            else QPainter.CompositionMode_DestinationOut)
    rows = max(1, _STRIP_PIXELS // w)
    parts = []
    for y in range(0, h, rows):
        n = min(rows, h - y)
        part = slice(y * w, (y + n) * w)
        parts.append(_scaled_alpha(alpha_bytes[part], mask[part], w, n, mode))
    new_alpha = b"".join(parts)
    if len(new_alpha) != w * h:
        raise CommandError("krita_error", f"Masking gave {len(new_alpha)} alpha bytes for {w}x{h}")
    return new_alpha


def require_rgba8(doc):
    """Raise ``conflict`` unless ``doc`` is 8-bit RGBA, the only color
    space the pixel tools support (D5)."""
    model, depth = doc.colorModel(), doc.colorDepth()
    if (model, depth) != RGBA8:
        raise CommandError(
            "conflict",
            f"The document is {model}/{depth}; pixel tools need an 8-bit RGBA document "
            "(RGBA/U8). Convert it in Krita (Image > Convert Image Color Space)",
            {"color_model": model, "color_depth": depth})
