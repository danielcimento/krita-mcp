# Krita Python API notes

Notes on Krita's Python scripting API (`libkis`), for plugin work. The
target is **Krita 5.3.4**, the version installed on the development
machine.

The authoritative list of signatures is the type stub that ships with
Krita:

```
/Applications/krita.app/Contents/Frameworks/krita-python-libs/PyKrita/krita.pyi
```

Read it before using an API you haven't used before. Online API docs are
often a version behind.

Items are tagged:

- **[stub]**: the signature exists in the 5.3.4 stub. Behaviour is not verified.
- **[verified]**: behaviour confirmed by a run in Krita. Note the date and version.
- **[to verify]**: an assumption that needs a test. The F001 probe script,
  [f001_buildout/spike/krita_api_probe.py](f001_buildout/spike/krita_api_probe.py),
  covers most of these. Move each item to *verified* (or correct it) when
  its result is in.

## Environment

- Krita's bundled Python is **3.13**, with **PyQt5** but **no numpy** [verified 2026-11-24, from the app bundle contents].
  Pixel code has to work on plain `bytes`/`bytearray` and `QImage`.
  An optional numpy fast path is fine; requiring numpy is not.
- `kritarunner` ships in the macOS bundle (`krita.app/Contents/MacOS/kritarunner`).
  Whether it can host our modules for headless tests is [to verify].

## Threading

- Call libkis only from the main thread. The plugin's command queue and
  `QTimer` exist for this reason.
- Long operations block the UI. libkis has no progress or cancel hook.

## Documents

- `Krita.instance().documents()` lists open documents [stub]. `Document`
  defines `__eq__` [stub], so compare documents with `==`, never `is`:
  each call returns a new Python wrapper.
- `Document` has no stable ID. Handles have to be issued and tracked by
  the plugin.
- `Document.name()` is the document *title* metadata, which is empty for
  many imported files (seen with `.ora`). Fall back to the basename of
  `fileName()`.
- `createDocument(w, h, name, "RGBA", "U8", profile, dpi)` [stub]. Whether
  it creates a default layer is [to verify].
- `saveAs(path)` saves natively and re-points `fileName()`. `exportImage(path, InfoObject())`
  writes a copy and leaves `fileName()` alone. Both need
  `setBatchmode(True)` to avoid export dialogs. Whether either can still
  show a dialog in batch mode is [to verify].
- `projection(x, y, w, h) -> QImage` gives the composited image of a region [stub].
  It is the cheapest way to render without writing a file.
- `refreshProjection()` then `waitForDone()` before reading pixels after a
  change.
- `clone() -> Document` [stub]: a full copy, useful for rendering with
  temporary visibility changes. Its cost on a 100 MB document, and
  whether it keeps node UUIDs, are [to verify].
- `resizeImage(x, y, w, h)` changes the canvas without scaling.
  `scaleImage` scales content. `crop` crops.
- `close()` on a modified document may prompt. Call `setModified(False)`
  first when the caller has asked to discard changes. Whether it still
  prompts after that is [to verify].
- `colorModel()` / `colorDepth()` / `colorProfile()` return strings such as
  `"RGBA"`, `"U8"`, `"sRGB-elle-V2-srgbtrc.icc"`.

## Nodes

- Type strings come from `Node.type()`: `paintlayer`, `grouplayer`,
  `filterlayer`, `filllayer`, `clonelayer`, `vectorlayer`, `filelayer`,
  `transparencymask`, `filtermask`, `transformmask`, `selectionmask`,
  `colorizemask`.
- `uniqueId() -> QUuid` and `Document.nodeByUniqueID(QUuid)` [stub].
  Serialize with `QUuid.toString()` (braced form) and parse with
  `QUuid(str)`. Persistence in `.kra` is [to verify]. OpenRaster and PSD
  do not store Krita UUIDs, so expect new IDs after reopening those formats.
- `childNodes()` is ordered **bottom to top**. `index()` is the position
  in that list.
- `addChildNode(child, above)` inserts `child` directly above `above`, or
  at the bottom when `above` is `None`. Confirm the `None` case [to verify].
  To reorder, `removeChildNode` from the old parent, then `addChildNode`.
  `setChildNodes(list)` replaces all children at once.
- `bounds() -> QRect`: whether this is exact or tile-aligned is
  [to verify]. Lint and export cropping depend on it.
- `opacity()` / `setOpacity()` use **0–255**, not 0–1 or percent.
- Blend mode IDs are lowercase strings: `normal`, `multiply`, `add`,
  `screen`, … (`blendingMode()` / `setBlendingMode()`).
- `inheritAlpha()` is Krita's clipping ("alpha inheritance").
- `GroupLayer.passThroughMode()` [stub].
- `layerStyleToAsl()` returns the layer style as ASL XML. It is expected
  to be empty when the layer has no style [to verify].
- `duplicate()`, `mergeDown()`, `move(x, y)`, `scaleNode`, `rotateNode`,
  `shearNode` and `cropNode` exist [stub]. Pivot and coordinate semantics
  are [to verify].
- `Node.save(filename, xRes, yRes, InfoObject, exportRect)` writes one
  node to an image file [stub].
- `paintLine` / `paintRectangle` / `paintPolygon` / `paintPath` paint with
  the current brush preset [stub]. They could replace the plugin's
  hand-rolled rasterizer later.

## Pixels

- For 8-bit RGBA documents, `pixelData()` / `setPixelData()` bytes are
  **B, G, R, A** per pixel, non-premultiplied.
- `QImage.Format_ARGB32` on a little-endian machine has the same byte
  layout, also non-premultiplied. So
  `QImage(data, w, h, 4*w, QImage.Format_ARGB32)` wraps Krita bytes with no
  per-pixel work, and `img.convertToFormat(QImage.Format_ARGB32)` then
  `img.constBits().asstring(img.sizeInBytes())` goes the other way. Keep the
  `bytes` object alive while a `QImage` wraps it. [to verify]: round trip
  through PNG is bit-exact.
- Building a buffer with `bytes([b, g, r, a] * n)` first allocates a
  Python list of 4·n ints (8-byte pointers, about 1.6 GB for 48.7 MP)
  before copying it. Use
  `bytes((b, g, r, a)) * n` instead.
- Fast whole-buffer tricks without numpy: `data[3::4]` extracts alpha,
  `bytes.translate(table)` maps values through a 256-entry table, and
  `bytearray[3::4] = ...` writes a channel back.
- Whether `setPixelData` adds an undo step is [to verify]. If it doesn't,
  the painting tools already bypass undo.

## Selections

- `Selection` has `select(rect)`, `selectAll(node, value)`, `invert`, `add`,
  `subtract`, `intersect`, `grow`, `shrink`, `feather`, `copy(node)`,
  `cut(node)`, `paste(node, x, y)`, and 8-bit `pixelData` / `setPixelData`
  [stub].
- `Document.selection()` / `setSelection()` read and set the active selection.
- `grow` dilates in C++, which makes it usable for "pixels near the part"
  checks without a Python loop.

## Undo

- The 5.3.4 stub has **no undo or macro API**: no `beginMacro` and no
  undo stack access [verified 2026-11-24, by searching the stub].
- `Krita.instance().action("edit_undo").trigger()` undoes on the *active
  view's* document.
- Which libkis calls create undo steps (`addChildNode`, `setName`,
  `setVisible`, `setPixelData`, …) is [to verify]. The F001 design
  depends on the answer. See
  [f001_buildout/decisions.md § D6](f001_buildout/decisions.md#d6-undo-granularity).
