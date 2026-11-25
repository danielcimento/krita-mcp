# F001 API design

The contract for every tool after the buildout. The rationale is in
[decisions.md](decisions.md), and the request as received is in
[spec.md](spec.md).

## Conventions shared by all tools

**Names.** MCP tool `krita_<name>`; plugin action `<name>` (D1).

**Common parameters.**

| Param | Type | Meaning |
|---|---|---|
| `doc` | string, optional | Document handle (`doc-3`), or the absolute path of an open document. Defaults to the active document (D3) |
| `timeout_s` | number, optional | Overrides the command's default timeout, up to 900 (D9) |
| `dry_run` | bool, optional | On mutating batch tools: validate only (D11) |

**Layer refs.** A UUID, a `/`-separated name path from the root, or a
bare unique name. The root is `"/"` (D4).

**Paths.** Absolute, `~`-prefixed, or relative to the output dir. They
must resolve inside an allowed root (D7). Results always return the
resolved absolute path.

**Result envelope.** Tool-specific fields plus `summary` (one line) and
`elapsed_ms`. Errors raise `ToolError("<code>: <message>")` (D2).

**Units.** Opacity is `0.0–1.0` in the API (Krita uses 0–255 inside; the
plugin converts with `round(v * 255)`). Coordinates are canvas pixels, and
the origin is top-left. Rectangles are `{x, y, w, h}`. Blend modes are
Krita IDs (`normal`, `multiply`, `add`, …).

**Layer object.** Returned by `list_layers` and echoed by mutating tools:

```json
{
  "id": "{8c1f…}", "name": "Eyeball L", "path": "Head/Eyes/Eyeball L",
  "type": "paintlayer", "parent": "{51a2…}", "index": 3,
  "visible": true, "opacity": 1.0, "blend_mode": "normal",
  "locked": false, "alpha_locked": false, "inherit_alpha": false,
  "pass_through": null, "has_layer_style": false,
  "bounds": {"x": 2410, "y": 1930, "w": 212, "h": 160},
  "children": []
}
```

`parent` is `null` for top-level nodes. `index` counts from the bottom
within the parent, like Krita's `childNodes()`. `pass_through` is a bool
on groups and `null` otherwise. `children` lists the node's children
**top to bottom**, the order of Krita's Layers docker. The order is fixed
here because the two orders are easy to confuse. `bounds` is `null` for
nodes with no extent.

---

## Documents

### `krita_list_documents()` · P0

```json
{"documents": [{"doc": "doc-1", "name": "mao", "path": "/Users/…/mao.kra",
  "width": 5800, "height": 8400, "modified": false, "active": true}], "summary": "…"}
```

`name` falls back to the basename of `path` when Krita's title is empty.

### `krita_get_document_info(doc?)` · P0

Returns `doc`, `name`, `path`, `width`, `height`, `resolution` (dpi),
`color_model`, `color_depth`, `color_profile`, `modified`,
`layer_counts` (`{"paintlayer": 184, "grouplayer": 38, …}`, every type
present), `max_depth`, and `selection` (`null`, or the bounding rect of the
active selection).

### `krita_new_canvas(width, height, name?, background?, color_profile?, resolution?)` · P0

- Limits: 16384 per side and 67.1 MP total (D8).
- `background`: a hex color, or `"transparent"`. `createDocument` already
  makes one opaque white paint layer named `Background` (spike Q4). That
  layer is reused, with no second layer added as the current code does. It is filled with the color, or,
  for `"transparent"`, replaced with an empty paint layer of the same
  name, which is cheaper than clearing 48 MP.
- `color_profile`: defaults to Krita's default sRGB profile for RGBA/U8.
  Profiles that aren't installed fail with `bad_request`, and the error
  lists the available profiles.
- Returns `doc`, `name`, `width`, `height`, `color_profile` and
  `background_layer` (its ID).
- The fill uses `bytes((b, g, r, 255)) * n`. The current code builds a
  Python list first, about 1.6 GB of pointers at 48.7 MP.

### `krita_open_file(path)` · P0

Returns `doc`, `path` (resolved), `name` (with the basename fallback),
`width`, `height`, and `layer_counts`. If the file is already open, the
existing handle comes back with `already_open: true` and the file is not
opened a second time.

### `krita_save(doc?, path?, format?)` · P0

- No `path`: save in place (`Document.save()`). This fails with `conflict`
  if the document has never been saved.
- `format` defaults to the extension of `path`. `kra` uses `saveAs`, so
  the document's file path moves to the new location. Other formats use
  `exportImage` in batch mode, and the document's own path is unchanged.
- Returns `path` (resolved), `format`, `bytes` (file size),
  `doc_path_changed`, and `elapsed_ms`.

### `krita_close_document(doc, discard=false)` · P1

Fails with `conflict` when the document is modified and `discard` is
false. With `discard`, the plugin calls `setModified(False)` then
`close()`. The handle is retired.

### `krita_set_canvas_size(doc, width, height, anchor="center")` · P2

`anchor` is one of `top-left`, `top`, `top-right`, `left`, `center`, `right`, `bottom-left`,
`bottom`, `bottom-right`. Uses `resizeImage(x, y, w, h)` with the offset
the anchor gives. Content is not scaled. The `summary` warns that a canvas
resize breaks existing rigs (CONVENTIONS: *Canvas*).

---

## Layer tree

### `krita_list_layers(doc?, root?, depth?, fields?)` · P0

- `root` is a layer ref, default `/`. `depth` is unlimited by default;
  `depth=1` lists direct children only.
- The document's global selection, a root-level `selectionmask` (spike
  Q9), is not listed. `get_document_info.selection` reports it.
- `fields` is an optional subset of layer-object keys. It keeps responses
  small for big documents. `id`, `name` and `children` are always included.
- Returns `{"layers": [<layer object>, …], "counts": {...}, "summary": …}`.
  `layers` is top to bottom.

### `krita_create_layers(doc?, specs | manifest, dry_run?)` · P0

Pass either `specs` (a list) or `manifest` (the path of a `manifest.json`;
D13). Each spec:

| Field | Type | Default | Notes |
|---|---|---|---|
| `key` | string | — | Batch-local name so later specs can refer to this layer as `parent`/`above` with `"$<key>"` |
| `name` | string | required | |
| `type` | `"paint"` \| `"group"` | `"paint"` | |
| `parent` | ref \| `"$key"` | `"/"` | |
| `above` | ref \| `"$key"` | — | Insert directly above this sibling. If omitted, the layer goes **on top** of the parent's existing children |
| `visible` | bool | `true` | |
| `opacity` | number | `1.0` | |
| `blend_mode` | string | `"normal"` | Checked against a fixed list of Krita composite-op IDs in the plugin. `setBlendingMode` stores any string, invalid ones included (spike Q15) |
| `locked`, `alpha_locked`, `inherit_alpha` | bool | `false` | |
| `pass_through` | bool | `false` | Groups only |
| `png` | path | — | Paint only. Read with QImage and converted to BGRA |
| `x`, `y` | int | `0` | Canvas offset of the PNG's top-left corner |

Order: specs are applied in list order, and each goes on top of its
parent's children unless `above` is given. So **listing siblings bottom
to top** reproduces a stack, and that is the order the manifest uses.

Returns `{"created": [{"key", "id", "name", "path"}], "summary": …}`.
On failure, nothing stays created (D11).

Performance: spike Q11 puts `setPixelData` at under 40 ms even for a
full 48 MP layer. So the 184-layer Mao build is bounded by PNG decoding
(cropped PNGs, mostly far smaller than the canvas), and should take
seconds. The default timeout for this command is 300 s.

### `krita_update_layers(doc?, changes, dry_run?)` · P0

Each change is `{id, name?, visible?, opacity?, blend_mode?, locked?,
alpha_locked?, inherit_alpha?, pass_through?}`. Only the given fields
change. A rename that would create a document-wide duplicate is allowed,
but the result lists it in `warnings` (lint catches it later).

### `krita_move_layers(doc?, moves, dry_run?)` · P0

Each move is `{id, parent?, above? | below? | index?}`, with at most one
position field. `parent` defaults to the current parent. With no position
field, the layer goes on top of the new parent. Moving a group into its own
subtree fails with `bad_request`. Implemented as `removeChildNode` +
`addChildNode`. Bounds are unchanged, since moves never shift pixels.

### `krita_delete_layers(doc?, ids, dry_run?)` · P0

Deleting a group deletes its subtree. Returns the deleted IDs, with
subtree members included.

### `krita_duplicate_layer(doc?, id, name?, parent?, above?)` · P1

`Node.duplicate()`. The copy goes directly above the source unless
`parent`/`above` say otherwise. Returns the new layer object. The default
name is Krita's (`<name> (copy)`), and the result warns about the
duplicate-name convention.

### `krita_merge_layers(doc?, ids, name)` · P1

- One group ID: flatten the group into one paint layer at its position.
  The group's projection becomes the new layer's pixels, which bakes in
  clipping, filters, masks and layer styles inside the group. The new
  layer keeps the group's opacity and blend mode.
- Several IDs: they must share a parent. They are moved, in their current
  stacking order, into a temporary group at the topmost one's position.
  The group is then flattened as above. This turns "bake this clipped
  layer into its base" into one call.
- Returns the new layer object and the removed IDs.
- A group's projection includes inherit-alpha clipping inside it, but
  not the group's own opacity (spike Q8). The flattened layer therefore
  takes the group's opacity and blend mode as properties, and doesn't bake
  them into its pixels.

### `krita_set_active_layer(doc?, id)` · P1

`Document.setActiveNode`. When `doc` is not the active document, the
result says that painting tools with no `doc` will still go to the active
document.

---

## Pixels

### `krita_export_layers(doc?, ids="all", dir, crop=true, include_hidden=true, include_groups=false, overwrite=false)` · P0

- Writes one PNG per paint layer. File names are
  `<index:04d>_<slug(name)>.png`, unique even with duplicate names. With
  `crop`, each PNG is cut to the layer's opaque bounds. Without it, each is
  canvas-sized at offset (0, 0).
- With `include_groups`, it also writes each group's projection, marked
  `"projection": true` in the manifest. `create_layers` skips those
  entries.
- Writes `manifest.json` (below) to `dir`.
- Empty layers get a manifest entry with `png: null`, so the structure
  still round-trips.
- Returns `dir`, `manifest` (path), `count`, `skipped` (with reasons) and
  `bytes`.

**Manifest schema (`version: 1`).**

```json
{
  "version": 1,
  "generator": "krita-mcp",
  "document": {"name": "mao", "width": 5800, "height": 8400, "resolution": 300,
               "color_model": "RGBA", "color_depth": "U8", "color_profile": "sRGB-elle-V2-srgbtrc.icc"},
  "layers": [
    {"key": "L0001", "id": "{…}", "name": "Hair Back", "path": "Hair Back",
     "type": "group", "parent": "/", "visible": true, "opacity": 1.0,
     "blend_mode": "normal", "pass_through": false, "stack_index": 0},
    {"key": "L0002", "id": "{…}", "name": "Hair Back Shadow Multiply", "path": "Hair Back/Hair Back Shadow Multiply",
     "type": "paint", "parent": "$L0001", "visible": true, "opacity": 1.0,
     "blend_mode": "multiply", "inherit_alpha": false, "alpha_locked": false, "locked": false,
     "png": "0002_hair-back-shadow-multiply.png", "x": 812, "y": 640, "w": 2310, "h": 1904,
     "stack_index": 1}
  ]
}
```

- `layers` is in create order: depth-first, and within each parent from
  bottom to top. Every parent comes before its children. `stack_index` is
  the global draw order, where 0 draws first and sits furthest back.
- `png` is relative to the manifest's directory. `create_layers(manifest=…)`
  resolves it there and checks it against the allowed roots like any other
  path.
- `id` is informative only. `create_layers` assigns new IDs.
- The manifest is a superset of the `create_layers` spec, so any spec
  field can appear in it.

### `krita_import_png(doc?, id, png, x=0, y=0, mode="replace")` · P0

- The target must be a paint layer, otherwise the call fails with `conflict`.
- `replace`: the PNG's rectangle overwrites the layer's pixels exactly,
  alpha included. Pixels outside the rectangle are untouched.
- `over`: source-over compositing onto the existing pixels within the
  rectangle (`QPainter`, `CompositionMode_SourceOver`).
- `clear_outside: true` (with `replace`): first clear the layer's whole
  extent, so the layer then contains only the PNG.
- Returns the changed rectangle and the layer's new bounds.

### `krita_render(doc?, path, region?, scale=1.0, only?, hide?, background?)` · P0

- `region` is `{x, y, w, h}` in canvas pixels, default the whole canvas. It is clipped to the canvas.
- `scale` is in (0, 4]. Resampling is smooth, with `Qt.SmoothTransformation`.
- `only` is a list of refs. Everything else is hidden for the render. Ancestors of `only` layers are made visible, and
  descendants of an `only` group keep their own visibility. `hide` is a
  list of refs hidden for the render. Combining both means: only these,
  minus those.
- `background`: `null` for transparent (the default), or a hex color composited underneath.
- With `only` / `hide`, the render happens in a `clone()` of the document
  (≈10 ms even for Mao), so the user's document is never touched (D10).
- Cost is mostly PNG encoding, ≈1.7 s per full Mao-size image (Q11).
  Use `scale` (e.g. 0.25) for previews.
- `png_level`: optional zlib level 0–9 for the output (default Qt's).
  Lower is faster and bigger. The same option exists on `export_layers`.
- Returns `path`, `width`, `height` and `elapsed_ms`.
- `krita_get_canvas(filename)` stays as a shortcut for
  `render(path=<output dir>/<filename>)`.

### `krita_trim_layers(doc?, ids, alpha_threshold=1, near_px=null, dry_run?)` · P1

Sets pixels with `alpha < alpha_threshold` to fully transparent. With
`near_px`, only pixels further than `near_px` from any pixel with
`alpha ≥ alpha_threshold` are cleared, which keeps anti-aliased edges
(spike Q10: `Selection.grow`). Returns, per layer, the pixels cleared and
the old and new bounds.

### `krita_layer_stats(doc?, ids, buckets=[0,1,8,32,128,255])` · P1

Per layer: `bounds` (Krita), `opaque_bounds` (the tight rectangle around
`alpha > 0`), `pixel_count` (`alpha > 0`), `opaque_count` (`alpha = 255`),
`alpha_histogram` (counts per bucket edge), `empty`, and `beyond_canvas`
(the parts of `opaque_bounds` that lie outside the canvas). These feed lint.

---

## Selections and part separation (P1)

### `krita_select(doc?, mode, op="replace", ...)`

| `mode` | Extra params |
|---|---|
| `rect` | `x, y, w, h` |
| `polygon` | `points: [[x, y], …]`, `antialias=true`. Rasterized with `QPainter` into an 8-bit mask |
| `alpha_of` | `layer` (ref), `threshold=1` |
| `invert` | — |
| `none` | — (deselect) |
| `all` | — |

`op` is `replace` \| `add` \| `subtract` \| `intersect`. Optional
`grow`/`shrink`/`feather` radii apply after combining. Returns the
selection's bounds and its pixel count.

### `krita_selection_to_layer(doc?, source, name, parent?, above?, cut=true)`

Copies, or with `cut` moves, the source layer's pixels inside the current
selection to a new paint layer. The new layer goes directly above the
source unless `parent`/`above` say otherwise. Partial selection values
scale alpha, so feathered selections give soft edges. Pixels stay at the
same canvas position. Done in `pixels.py` with selection `pixelData`
times layer alpha. `Selection.cut`/`paste` do work (spike Q9), but they
go through the clipboard and would overwrite the user's.
Returns the new layer and the cut rectangle.

### `krita_transform_layer(doc?, id, translate?, scale?, rotate?, flip?, pivot?, resample="Bicubic")`

- `translate: [dx, dy]` is relative. It uses `Node.move` with the position plus the delta.
- `scale: [sx, sy]` or a number, `rotate` in degrees, clockwise.
- `flip: "h" | "v"` mirrors pixels exactly, with no resampling, using
  `QImage.mirrored` on the layer's bounds.
- `pivot: [x, y]` in canvas pixels, or `"center"` (the default, the layer
  bounds' center).
- Order: flip, scale, rotate, translate.
- Native calls (spike Q6): `move` sets an absolute offset, so translate
  adds the delta to `position()`. `scaleNode(origin, w, h, filter)` scales
  the bounds to `w × h` and keeps `origin` fixed, so `origin` = pivot.
  `rotateNode` rotates about the bounds' center, so a custom pivot is
  rotate, then `move` by the pivot's displacement. Positive radians turn
  clockwise on screen (spike Q6), so `rotate` in degrees maps directly to
  `rotateNode(radians(rotate))`.
- `resample` takes Krita's filter names: `Bicubic` (default), `Bilinear`,
  `NearestNeighbor`, `Lanczos3`, `Mitchell`, `BSpline`, `Bell`, `Hermite`.

---

## Analysis

### `krita_lint(doc?, rules?, severity_min?)` · P0

`rules` is a list of rule IDs and/or set names (`default`, `conventions`,
`all`). The default is `default`. Returns:

```json
{"findings": [{"rule": "node-type", "severity": "error", "layer_id": "{…}",
               "path": "Face/Blur", "message": "Filter layer 'Blur' must be baked or removed"}],
 "counts": {"error": 3, "warning": 1, "info": 0}, "rules_run": [...], "summary": "…"}
```

Findings are sorted by layer stacking order. Document-level findings have
`layer_id: null`. Findings that involve several layers add `related_ids`.

#### Lint rules

These restate the art repo's CONVENTIONS.md as checkable rules.
**Pixel rules apply only to paint layers.** The contents of reference
groups (name wrapped in brackets, `[ Guide ]`) are skipped by every rule
except `reference-hidden`.

`default` set: the spec's list, and what acceptance test 4 checks.

| Rule | Sev | Fires when |
|---|---|---|
| `node-type` | error | A node's type is not `paintlayer`/`grouplayer`: filter, fill, clone, vector or file layers, and all masks. Masks name their parent layer. The document's global selection (a root-level `selectionmask`) is exempt |
| `layer-style` | error | `layerStyleToAsl()` is not empty |
| `inherit-alpha` | error | `inherit_alpha` is true (Krita's clipping): bake it into the target with `merge_layers` |
| `blend-mode` | error | A paint layer's blend mode is not `normal`/`multiply`/`add`. Groups are exempt, since group blending doesn't matter to the rig. `linear_dodge` is reported with a hint to use `add`, the ID the rig's Add maps to (to confirm on the umamo side) |
| `duplicate-name` | error | Two or more nodes, anywhere in the document, share a name. **One finding per name.** `layer_id` is the second occurrence in stacking order, and `related_ids` lists all of them |
| `side-token` | warning | Within one parent, stripping side tokens makes names collide (`Eyebrow` next to `Eyebrow L`): flag the ones without a token. Also flags non-standard tokens such as `_L`, `Left`, `(L)` or a lowercase ` l` |
| `empty-layer` | warning | A paint layer with no pixel where `alpha > 0` |
| `out-of-canvas` | warning | A paint layer's `opaque_bounds` reaches outside `(0, 0, width, height)` |
| `stray-alpha` | warning | A paint layer has pixels with `0 < alpha < 8` further than 4 px from any pixel with `alpha ≥ 8`. Both thresholds are configurable. Anti-aliased edges don't count |
| `color-space` | error | The document is not `RGBA` / `U8`, or its profile name doesn't contain `sRGB`. One document-level finding |

`conventions` set: opt-in.

| Rule | Sev | Fires when |
|---|---|---|
| `nesting-depth` | warning | A paint layer is more than 3 groups deep |
| `name-case` | info | A name is not Title Case with space-separated words (side tokens, `A`/`B` variants and numbers are allowed) |
| `blend-suffix` | warning | A Multiply/Add layer's name doesn't end in ` Multiply`/` Add`, or a name has the suffix but a different mode |
| `pose-visibility` | warning | Among sibling groups named `<X> A`, `<X> B`, …, `A` is hidden or another one is visible |
| `reference-hidden` | warning | A bracketed reference group is visible |
| `mask-suffix` | info | A layer referenced as a clipping base has no ` Mask` suffix. It only fires together with `inherit-alpha` |

Rules are pure functions in `lint.py` over a snapshot (D14):
`snapshot -> [finding]`. Adding a rule means adding a function and a test,
with no Krita code involved.

### `krita_occlusion_map(doc?, id, path, modes=["normal"], threshold=0.5)` · P1

- "Above" means visible paint layers later than `id` in global draw order,
  inside visible ancestors. By default only `normal` layers count:
  Multiply and Add layers above tint the target, but don't hide it.
- Coverage is the alpha of the composite of those layers, restricted to
  the target's bounds (rendered as in `render(only=above)`).
- Writes a grayscale PNG the size of the target's bounds, where value =
  target alpha × coverage. Returns `bounds`, `covered_pct` (the share of
  the target's `alpha > 0` pixels with coverage ≥ `threshold`), and
  `fully_hidden` (bool).

### `krita_diff_documents(doc_a, doc_b, pixels=true)` · P2

Matches nodes by UUID first, then by name path, then by unique name.
Returns `added`, `removed`, `renamed`, `moved` (parent or index changed),
`changed` (property deltas), and, with `pixels`, `pixel_changes`
(`{id, changed_count, bounds}` from comparing `pixelData` over the union
of both bounds). The two documents must be the same size for pixel diffs.

---

## Existing tools after the buildout

| Tool | Change |
|---|---|
| `krita_health` | Adds `version`, `krita_version`, `allowed_dirs`, `config_errors`, `busy` |
| `krita_new_canvas`, `krita_open_file`, `krita_save` | As above |
| `krita_get_canvas` | Becomes a shortcut for `render` into the output dir |
| `krita_set_color` | Also stored plugin-side (D15). Accepts `doc` |
| `krita_set_brush`, `krita_list_brushes` | Structured results only |
| `krita_stroke`, `krita_fill`, `krita_draw_shape`, `krita_clear` | Accept `doc`, `layer`. Structured results. `clear` takes `"transparent"` |
| `krita_get_color_at` | Accepts `doc`, `layer` (sample one layer instead of the composite). Bounds checked against the document size |
| `krita_undo` / `krita_redo` | Become the agent journal (D6). The UI-stack triggers move to `krita_ui_undo` / `krita_ui_redo` |
