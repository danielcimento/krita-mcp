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
  2026-11-24 ([raw results](f001_buildout/spike/probe-results-run2.json)),
  and repeated in run 3 ([raw results](f001_buildout/spike/probe-results-run3.json)).
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
  `==` itself is safe on a closed wrapper. It returns no match
  [verified, run 3, Q17]. So a stored wrapper may be kept for `==`
  checks, but never called directly.
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
- `kritarunner` (`krita.app/Contents/MacOS/kritarunner`) runs one Python
  function in a headless Krita and exits [verified 2026-11-24, Q12]:
  - Usage: `kritarunner -s <module> -f <function> [args…]`, with the
    module name given without `.py`. The function is called with **one
    positional argument, the list of extra args**: `def run(args): …`.
  - It has its **own profile**, `~/Library/Application Support/kritarunner/`
    (resources, `kritarunnerrc`), separate from the GUI Krita's. On first
    run it builds a fresh resource database. It imports `<module>` only
    from that profile's `pykrita/` folder. `PYTHONPATH` is printed in the
    log but ignored for imports.
  - No windows (`len(app.windows()) == 0`), so extensions' `createActions`
    never runs. `createDocument`, `setPixelData`, batch-mode `saveAs`,
    `openDocument` and `close` all work. Exit is quick (3–4 s, including
    startup).
  - Harmless log noise: "has no json!" for the preview and thumbnailer
    appex, Fontconfig errors, many `kf.i18n` warnings, a swap-location
    fallback, "some tiles have leaked".
  - It doesn't conflict with a running GUI Krita.
  - **Two `kritarunner` processes at once on the same profile do
    conflict**: the second hung at startup after
    `UNIQUE constraint failed: storages.location` / `Could not add new
    storage "memory" to the database` in the shared resource database
    [verified 2026-11-25]. Run headless suites one at a time.
  - **Its exit code is always 0**: on test failures, when the module
    can't be imported, and when the function returns nonzero. Python's
    stdout reaches kritarunner's stdout, mixed into the log noise. So
    `make test-krita` judges the run by a marker line the shim prints
    (`KMCP_TEST_RESULT: PASS|FAIL`), and a missing line counts as failure
    [verified 2026-11-24, P1.1].

## Performance at Live2D size

Spike Q11, 5800×8400 (48.7 MP), Krita 5.3.4 on an M-series Mac
[verified 2026-11-24, `f001_buildout/spike/probe-results-q11.json`]:

| Operation | ms |
|---|---|
| `createDocument` | 37 |
| `bytes((b, g, r, a)) * n` (195 MB buffer) | 11 |
| Full-layer `setPixelData` / settle / `pixelData` | 39 / 23 / 20 |
| `data[3::4]` (alpha slice of 195 MB) | 17 |
| `projection()` full canvas, one layer / Mao | 27 / 17 |
| `clone()` one layer / Mao | 4 / 10 |
| **QImage → PNG, full canvas** (solid fill) | **1702** |
| `exportImage` PNG / `saveAs` `.kra` | 1336 / 1858 |
| Open Mao `.ora` (184 layers) + settle | 2633 + 77 |

Krita's own pixel operations are effectively free at this size. **PNG
encoding dominates**, and real art compresses worse than a solid fill.
Keep PNG writes to what the caller asked for, and prefer scaled renders
for previews. QImage's `save(path, "PNG", quality)` trades size for speed
(Qt maps quality to the zlib level). Whole-buffer `bytes` operations
are fast enough that per-pixel Python loops are never justified.

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
- A document from `createDocument` is owned by the Python wrapper it
  returns. With no view added (headless, no window), dropping the last
  reference destroys it: it disappears from `documents()` [verified
  2026-11-24, kritarunner]. So headless `new_canvas` returns `ok` but
  leaves nothing open; a handle registry must keep the wrapper.
- **Headless, `activeDocument()` is always `None`**: with documents open,
  and also right after `setActiveDocument(doc)` [verified 2026-11-24,
  kritarunner, P1.6]. So tools called without `doc` get `not_found`
  headless, and tests pass a handle.
- `saveAs(path)` sets `fileName()` to `path` exactly as given, not
  resolved: a temp dir under `/var/folders/…` stays `/var/…`, although
  its realpath is `/private/var/…`. Compare paths after `os.path.realpath`
  [verified 2026-11-24, P1.6]. Two documents saved to one path both
  report it.
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
- **`saveAs` and `save` can report success without writing.** Into a
  directory that doesn't exist, `saveAs(path)` returns `True`, writes
  nothing, and still re-points `fileName()` and clears `modified`;
  `save()` returns `True`, writes nothing and leaves `modified` set.
  `exportImage` returns `False` there. So check the file afterwards
  (`kritamcp/fileio.py` compares its stat before and after) [verified
  2026-11-25, kritarunner, P1.7].
- `save()` rewrites the file even when the document isn't modified
  [verified, P1.7].
- `openDocument` returns `None` for a file it can't read (garbage bytes
  named `.png`), with no dialog under app-wide batch mode. The document
  it returns has `batchmode()` false. A PNG opened this way has an empty
  `name()` and one paint layer named `Background` [verified, P1.7].
- The app-wide `batchmode()` is `False` by default (kritarunner)
  [verified, P1.7].
- `resolution()` is an `int`, rounded: a document created at 72.5 dpi
  reads 73, while `xRes()` gives 72.5 [verified, P1.7].
- `projection(x, y, w, h) -> QImage` gives the composited image of a region
  [verified as BGRA→RGB-correct].
- `refreshProjection()` then `waitForDone()` before reading pixels after a
  change.
- `clone()` copies the whole document, **keeping node UUIDs**. It is
  copy-on-write and nearly free: 1 ms for 256², 4 ms for a 5800×8400
  one-layer document, **10 ms for the 222-node Mao document**
  [verified, Q11].
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
  root-level `Selection Mask` [verified, Q9]. It goes away again after
  `setSelection(None)` [verified, run 3]. Tree listing and lint must treat
  a root-level `selectionmask` as UI state, not content.
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
- `childNodes()` and `nodeByUniqueID` return a `GroupLayer` for groups
  (so `passThroughMode()` is there, `False` by default) and a
  `SelectionMask` for the selection mask, but a plain `Node` for paint
  layers. `rootNode().type()` is `grouplayer`. The global `Selection
  Mask` is added **on top** (last in `childNodes()`). A group's
  `bounds()` is the union of its children's. `layerStyleToAsl()` is `''`
  for a group too [verified 2026-11-24, P1.6].
- **Headless, `Document.activeNode()` is always `None`**, also right
  after `setActiveNode(node)` [verified 2026-11-24, P1.6]. Painting tools
  without `layer` then fail with `not_found`.
- `addChildNode(child, above)` inserts `child` directly above `above`.
  With `above = None` it appends on top [verified: `add_paint` with
  `None` put the layer above `Background`]. To reorder, `removeChildNode`
  then `addChildNode(child, new_above)` [verified].
- `bounds()` is **exact**: a 10×10 write at (13, 17) gives
  `(13, 17, 10, 10)`. It shrinks to `(0, 0, 0, 0)` when the pixels are
  cleared to transparent. An empty layer gives `(0, 0, 0, 0)` with
  `hasExtents() == False`. But a layer whose pixels were zeroed with
  `setPixelData` has `bounds() == (0, 0, 0, 0)` while `hasExtents()`
  **stays `True`** [verified 2026-11-24, P1.6], so check the size, not
  only `hasExtents()`. Bounds are **not clipped to the canvas**:
  pixels written past the edge are kept and readable [verified].
- `opacity()` / `setOpacity()` use **0–255**.
- `setBlendingMode()` **accepts any string**: `"not-a-mode"` is stored
  and read back unchanged [verified]. Validate against a known list
  ourselves. `add` and `linear_dodge` are distinct IDs, and both are
  accepted. A layer with an invalid ID renders like Normal: 50% gray over
  white stayed 128 [verified, run 3]. That test can't tell Normal from
  Multiply, but either way an invalid ID fails silently.
- **`add` vs `linear_dodge`** [verified 2026-11-25, headless,
  `f001_buildout/spike/probe-results-blend.json`]:
  - Rendering is **byte-identical** at U8, U16 and F32, for opaque
    layers, partial source alpha and 50% layer opacity. At U8 both equal
    `min(255, src + dst)` per channel.
  - **`.psd` export loses `add`:** it is written as `norm` and reopens as
    `normal`. `linear_dodge` is written as `lddg` (Photoshop's "Linear
    Dodge (Add)") and reopens as `linear_dodge`.
  - `.ora` export: `add` → `svg:plus`, `linear_dodge` →
    `krita:linear_dodge` (a Krita-only op other ORA readers won't know).
    Both reopen unchanged in Krita.
  - `.kra` keeps both.
- `inheritAlpha()` is Krita's clipping ("alpha inheritance").
- A node removed with `remove()` can be put back: `addChildNode(node, …)`
  re-inserts the same wrapper with the same UUID, pixels, bounds, opacity
  and parent, and `nodeByUniqueID` returns `None` while it is out
  [verified, run 3, Q18].
- `GroupLayer.passThroughMode()` / `setPassThroughMode()` round-trip;
  `createGroupLayer(name)` returns a `GroupLayer` [verified 2026-11-25,
  P2.3].
- A **locked** layer (`setLocked(True)`) still takes `setPixelData`
  headless, and `remove()` removes it [verified 2026-11-25, P2.3].
  `create_layers` sets `locked` last anyway.
- **ORA import, groups and blend ops** [verified 2026-11-25, P2.6]:
  - In `.kra` `maindoc.xml` a group's flag is `passthrough="1"`/`"0"`.
    Krita imported the Mao `.ora` (38 groups, all `isolation="auto"`) with
    every group `passthrough="1"`. Its ORA importer sets pass-through only
    for `isolation="auto"`; `"isolate"` and a missing attribute give
    pass-through off. Its exporter writes `auto` for pass-through groups and
    `isolate` otherwise.
  - Importer `svg:` ops → Krita IDs: `src-over` `normal`, `multiply`,
    `plus` `add`, `screen`, `overlay`, `darken`, `lighten`, `color-dodge`
    `dodge`, `color-burn` `burn`, `hard-light` `hard_light`, `soft-light`
    **`soft_light_svg`** (not `soft_light`, which is the Photoshop variant),
    `difference` `diff`, `color`, `luminosity` **`luminize`**, `hue`,
    `saturation`, `dst-in` `destination-in`, `dst-out` `erase`, `dst-atop`
    `destination-atop`. `krita:<id>` → `<id>`. `svg:src-atop` is Normal with
    alpha locked. The exporter also writes `alpha-preserve="true"` (alpha
    lock with a non-Normal op), `edit-locked="true"` and `selected="true"`
    (sources: `plugins/impex/ora/kis_open_raster_stack_{load,save}_visitor.cpp`,
    `libs/pigment/KoCompositeOpRegistry.h`).
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
    (105, 45, 10, 20). **Positive radians turn clockwise on screen**
    (y down): a top-right marker at (119, 50) ended at (114, 64), bottom
    right [verified, run 3]. For a custom pivot, rotate and then `move` by
    the offset.
- `Node.save(filename, xRes, yRes, InfoObject, exportRect)` writes the
  node to a file, **with the layer's opacity applied**: alpha 200 at 50%
  opacity was saved as 100. It needs app-wide batch mode (see *Crash
  hazards*) [verified]. Not usable for raw pixel export.
- `paintLine` / `paintRectangle` / `paintPolygon` / `paintPath` paint with
  the current brush preset [stub]. They could replace the plugin's
  hand-rolled rasterizer later.

## Pixels

- `pixelData()` / `projectionPixelData()` return a `QByteArray`.
  Indexing it gives one-byte `bytes`, not `int`s, so convert with
  `bytes(...)` before reading values [verified 2026-11-24, P1.6: the old
  `get_color_at` failed with "unsupported format string passed to
  bytes.__format__"].
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
  [verified]. `cut` **puts the pixels on the system clipboard**
  (`application/x-krita-selection`, `application/x-qt-image`) [verified,
  run 3]. Don't use them in tools: they clobber the user's clipboard.
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
