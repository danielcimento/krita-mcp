# Krita Python API notes

Notes on Krita's Python scripting API (`libkis`), for plugin work. The
target is **Krita 5.3.4** (`5.3.4.1 (git bd6e8d0)`) on macOS arm64, the
version installed on the development machine.

The authoritative list of signatures is the type stub that ships with
Krita:

```
/Applications/krita.app/Contents/Frameworks/krita-python-libs/PyKrita/krita.pyi
```

Read it before using an API you haven't used before. Online API docs are
often a version behind.

Items are tagged:

- **[stub]**: the signature exists in the 5.3.4 stub. Behaviour is not verified.
- **[verified]**: behaviour confirmed by a run in Krita. Unless a note
  says otherwise, it was confirmed by the F001 probe, run 2, on
  2026-11-24 ([raw results](f001_buildout/spike/probe-results-run2.json)).
- **[to verify]**: an assumption that still needs a test. The probe script,
  [f001_buildout/spike/krita_api_probe.py](f001_buildout/spike/krita_api_probe.py),
  covers most of these.

## Crash hazards

These take Krita down with a segfault. Python's `try` can't catch that,
so the HTTP bridge dies with it.

- **Calling a method on a closed `Document`.** After `doc.close()`, or a
  user closing the document, any method call on an old wrapper
  (`setModified`, `name`, …) crashes. [verified, run 2: it crashed in the
  probe's cleanup, at address 0x10]. Before every use, check that the
  document is still open with `any(doc == d for d in Krita.instance().documents())`.
  Whether `==` itself is safe on a closed wrapper is being checked by
  probe Q17 [to verify]. The handle registry must never keep a `Document`
  beyond that check.
- **Modal dialogs during a command.** A dialog blocks the main thread, so
  the command never returns. Dismissing the one `Node.save` opened then
  crashed Krita [verified, run 1]. Every export path must run in batch
  mode. Use `doc.setBatchmode(True)` for `exportImage` / `saveAs`
  [verified: no dialog]. `Node.save` ignores the document flag and needs
  `Krita.instance().setBatchmode(True)` [verified]. Set the app-wide flag
  only around the call, in `try`/`finally`. Left on, it would suppress
  dialogs for the user's own exports.

## Environment

- Bundled Python is **3.13.5**, with **PyQt5 5.15** and **no numpy** [verified].
  Pixel code has to work on plain `bytes`/`bytearray` and `QImage`.
  An optional numpy fast path is fine; requiring numpy is not.
- Byte order is little-endian [verified].
- Scaling filter names for `scaleNode` / `scaleImage`: `Hermite`, `Bell`,
  `Lanczos3`, `Bilinear`, `Mitchell`, `NearestNeighbor`, `BSpline`,
  `Bicubic` [verified].
- `kritarunner` ships in the macOS bundle (`krita.app/Contents/MacOS/kritarunner`).
  Whether it can host our modules for headless tests is [to verify].

## Threading

- Call libkis only from the main thread. The plugin's command queue and
  `QTimer` exist for this reason.
- Long operations block the UI. libkis has no progress or cancel hook.

## Documents

- `Krita.instance().documents()` lists open documents. `Document ==`
  works for identity, but `is` does not, since every call returns a new
  wrapper [verified].
- `Document` has no stable ID. Handles have to be issued and tracked by
  the plugin.
- `Document.name()` is the document's title metadata. It is empty for
  documents opened from `.ora` [verified], so fall back to the basename of
  `fileName()`.
- `createDocument(w, h, name, "RGBA", "U8", "", dpi)` creates one paint
  layer, **`Background`**, filled opaque white. An empty profile gives
  `sRGB-elle-V2-srgbtrc.icc`. The new document has `fileName() == ""` and
  `modified() == False` [verified]. Note that the current `new_canvas` adds a
  *second* layer on top of it.
- `app.profiles("RGBA", "U8")` lists the 21 installed profiles,
  including `sRGB-elle-V2-srgbtrc.icc`, `sRGB built-in`, `sRGB IEC61966-2.1`
  [verified].
- Batch mode, `saveAs`, `exportImage` [verified]:
  - With `setBatchmode(True)`, no dialog appears, and both return `True`
    on success.
  - `exportImage` leaves `fileName()` alone. For an unsaved document it
    stays `""`.
  - `saveAs` re-points `fileName()` and clears `modified`.
- `close()` returns `True`, with no prompt, after `setModified(False)`
  [verified].
- `projection(x, y, w, h) -> QImage` gives the composited image of a region
  [verified as BGRA→RGB-correct].
- `refreshProjection()` then `waitForDone()` before reading pixels after a
  change.
- `clone()` copies the whole document, **keeping node UUIDs**. It took
  1 ms for a 256² document [verified]. The cost at Mao size is still
  [to verify] (Q11).
- `resizeImage(x, y, w, h)` changes the canvas without scaling.
  `scaleImage` scales content. `crop` crops [stub].
- `colorModel()` / `colorDepth()` / `colorProfile()` return `"RGBA"`,
  `"U8"`, `"sRGB-elle-V2-srgbtrc.icc"` [verified].

## Nodes

- Type strings come from `Node.type()`: `paintlayer`, `grouplayer`,
  `filterlayer`, `filllayer`, `clonelayer`, `vectorlayer`, `filelayer`,
  `transparencymask`, `filtermask`, `transformmask`, `selectionmask`,
  `colorizemask`.
- **The document's global selection shows up as a node.** After
  `Document.setSelection(sel)`, `rootNode().childNodes()` includes a
  root-level `Selection Mask` [verified, Q9]. Whether it goes away after
  `setSelection(None)` is [to verify] (Q9, run 3). Tree listing and lint
  must treat a root-level `selectionmask` as UI state, not content.
- UUIDs [verified]:
  - `uniqueId().toString()` gives the braced lowercase form, e.g.
    `{c0cc968f-d678-4a81-88fd-c9210f662cea}`.
  - `nodeByUniqueID(QUuid(s))` works with or without braces.
  - IDs survive `clone()` and a `.kra` save and reopen.
  - `.ora` does not keep them: every node gets a new ID on reopen.
- `Node ==` compares identity, e.g. a `nodeByUniqueID` result `==` the
  original wrapper [verified].
- `childNodes()` is ordered **bottom to top**. `index()` is the position
  in that list.
- `addChildNode(child, above)` inserts `child` directly above `above`.
  With `above = None` it appends on top [verified: `add_paint` with
  `None` put the layer above `Background`]. To reorder, `removeChildNode`
  then `addChildNode(child, new_above)` [verified].
- `bounds()` is **exact**: a 10×10 write at (13, 17) gives
  `(13, 17, 10, 10)`. It shrinks to `(0, 0, 0, 0)` when the pixels are
  cleared to transparent. An empty layer gives `(0, 0, 0, 0)` with
  `hasExtents() == False`. Bounds are **not clipped to the canvas**:
  pixels written past the edge are kept and readable [verified].
- `opacity()` / `setOpacity()` use **0–255**.
- `setBlendingMode()` **accepts any string**: `"not-a-mode"` is stored
  and read back unchanged [verified]. Validate against a known list
  ourselves. `add` and `linear_dodge` are distinct IDs, and both are
  accepted. How an invalid mode renders is in Q15, run 3 [to verify].
- `inheritAlpha()` is Krita's clipping ("alpha inheritance").
- `GroupLayer.passThroughMode()` [stub].
- `layerStyleToAsl()` returns `''` for a layer without a style
  [verified]. The non-empty case is [to verify], using the lint fixture.
- Group `projectionPixelData()` **includes** inherit-alpha clipping
  inside the group, and **excludes** the group's own opacity [verified, Q8].
  So flattening a group bakes the clipping, but the group's opacity has to
  be carried over to the new layer separately.
- Transforms [verified, Q6]:
  - `move(x, y)` sets the layer's **absolute offset** (`position()`), and
    pixels shift with it. Calling `move(10, 10)` twice moves once.
    A relative translate is `move(pos.x() + dx, pos.y() + dy)`.
  - `scaleNode(origin, w, h, filter)` scales so the layer's bounds become
    `w × h`, with `origin` staying fixed. Origin at the bounds' top-left
    kept that corner in place.
  - `rotateNode(radians)` rotates about the **center of the layer's
    bounds**, not the image center: (100, 50, 20, 10) became
    (105, 45, 10, 20). The direction is [to verify] (Q6, run 3). For a
    custom pivot, rotate and then `move` by the offset.
- `Node.save(filename, xRes, yRes, InfoObject, exportRect)` writes the
  node to a file, **with the layer's opacity applied**: alpha 200 at 50%
  opacity was saved as 100. It needs app-wide batch mode (see *Crash
  hazards*) [verified]. Not usable for raw pixel export.
- `paintLine` / `paintRectangle` / `paintPolygon` / `paintPath` paint with
  the current brush preset [stub]. They could replace the plugin's
  hand-rolled rasterizer later.

## Pixels

- For 8-bit RGBA documents, `pixelData()` / `setPixelData()` bytes are
  **B, G, R, A** per pixel, non-premultiplied. The round trip is exact,
  including partial and zero alpha [verified].
- `QImage.Format_ARGB32` has the same byte layout:
  `QImage(data, w, h, 4*w, QImage.Format_ARGB32)` wraps Krita bytes, and
  `QImage(path).convertToFormat(Format_ARGB32)` +
  `constBits().asstring(sizeInBytes())` reads them back. PNG save and load
  through QImage is **bit-exact**, including colors under zero alpha
  [verified, Q5]. Keep the `bytes` object alive while a `QImage` wraps it,
  or `.copy()` the image.
- Building a buffer with `bytes([b, g, r, a] * n)` first allocates a
  Python list of 4·n ints (8-byte pointers, about 1.6 GB for 48.7 MP)
  before copying it. Use `bytes((b, g, r, a)) * n` instead.
- Fast whole-buffer tricks without numpy: `data[3::4]` extracts alpha,
  `bytes.translate(table)` maps values through a 256-entry table, and
  `bytearray[3::4] = ...` writes a channel back.

## Selections

- `Selection` has `select(rect)`, `selectAll(node, value)`, `invert`, `add`,
  `subtract`, `intersect`, `grow`, `shrink`, `feather`, `copy(node)`,
  `cut(node)`, `paste(node, x, y)`, and 8-bit `pixelData` / `setPixelData`
  [stub].
- `Document.setSelection(sel)` / `selection()` round-trip the rectangle,
  and `setSelection(None)` makes `selection()` return `None` [verified].
- `cut(node)` clears the selected pixels in `node`, and
  `paste(dest, x, y)` writes them into `dest`, without creating a new layer
  [verified]. They probably go through the clipboard. Q9 run 3 records the
  system clipboard [to verify]. Don't use them in tools: they would
  clobber the user's clipboard.
- `grow(r, r)` dilates with a roughly round kernel. One pixel grown by 4
  became 69 pixels in a 9×9 box. After `resize(w, h)`,
  `setPixelData` / `pixelData` work on a standalone `Selection` [verified].
  It is usable for "pixels near the part" checks without a Python loop.

## Undo

The stub has **no undo or macro API**. Some libkis calls create undo
steps and others don't [verified, Q1: one `edit_undo` trigger after each
call, on a fresh document whose view was active]:

| Undoable | Not undoable (undo reverts the *previous* step instead) |
|---|---|
| `addChildNode`, `remove`, `setBlendingMode` | `setName`, `setVisible`, `setOpacity`, `setInheritAlpha`, `setPixelData`, `move`, `setActiveNode` |

A reorder (`removeChildNode` + `addChildNode`) takes at least two steps:
one undo only reverted the `addChildNode`, which left the layer removed.

Consequences:

- Krita's Edit → Undo after an agent edit is unreliable. It skips
  property and pixel changes, and may revert an earlier, unrelated
  structural change. This already affects the existing painting tools,
  which write with `setPixelData`.
- `Krita.instance().action("edit_undo").trigger()` acts on the *active
  view's* document.
- The F001 design uses an undo journal kept by the plugin instead. See
  [f001_buildout/decisions.md § D6](f001_buildout/decisions.md#d6-undo-granularity).
