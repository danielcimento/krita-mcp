"""Pixel I/O between PNG files and Krita layers (D5): QImage, no numpy.

Krita's 8-bit RGBA ``pixelData`` / ``setPixelData`` bytes are B, G, R, A
per pixel, non-premultiplied, which is ``QImage.Format_ARGB32``'s layout on
little-endian machines. PNG load and save through QImage is bit-exact,
colors under zero alpha included (spike Q5). No per-pixel Python loops.
"""

import struct
import sys
import zlib

from PyQt5.QtGui import QImage

from .errors import CommandError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HEADER_LEN = 8 + 4 + 4 + 13 + 4  # signature, IHDR length, type, data, CRC

RGBA8 = ("RGBA", "U8")


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


def load_png_bgra(path):
    """Decode a PNG into Krita's 8-bit BGRA bytes: ``(data, width, height)``.

    ``data`` is a ``bytes`` copy, ``4 * width * height`` long, rows top to
    bottom with no padding. Errors: ``bad_request`` if Qt cannot decode
    the file as a PNG.
    """
    if sys.byteorder != "little":  # ARGB32 is BGRA in memory only on little-endian
        raise CommandError("krita_error", "Pixel I/O needs a little-endian machine")
    image = QImage(path, "PNG")
    if image.isNull():
        raise CommandError("bad_request", f"Cannot decode PNG: {path}", {"path": path})
    image = image.convertToFormat(QImage.Format_ARGB32)
    width, height = image.width(), image.height()
    if image.bytesPerLine() != 4 * width:  # ARGB32 rows are 4-byte aligned: never padded
        raise CommandError("krita_error",
                           f"Unexpected row stride {image.bytesPerLine()} for width {width}")
    data = image.constBits().asstring(image.sizeInBytes())
    if len(data) != 4 * width * height:
        raise CommandError("krita_error", f"Decoded {len(data)} bytes for {width}x{height}")
    return data, width, height


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
