# F001 API design

The contract for every tool after the buildout. The rationale is in
[decisions.md](decisions.md), and the request as received is in
[spec.md](spec.md).

## Conventions shared by all tools

**Names.** MCP tool `krita_<name>`; plugin action `<name>` (D1).

**Common parameters.**

| Param | Type | Meaning |
|---|---|---|
| `doc` | string, optional | Document handle (`doc-3`), or the absolute path of an open document. Defaults to the active document (D3). Every tool that acts on one document takes it; tools that act on app or UI state (`health`, `set_color`, `set_brush`, `list_brushes`, `ui_undo`/`ui_redo`) don't. Details (P1.6, 2026-11-24): a path (absolute or `~`) is compared after `realpath` with each open document's `fileName()`; it only identifies an open document, so it isn't checked against the allowed roots. Several matches are `ambiguous` (`details.matches` lists their handles). With no `doc` and no active document (always so headless), `not_found`. Results of document tools include `doc` |
| `timeout_s` | number, optional | Overrides the command's default timeout (30 s for queries, 300 s for file I/O), up to 900 (D9). Every tool except `krita_health`. Sent as `params.timeout_s`; the plugin checks 0 < t ≤ 900 (else `bad_request`) and strips it before the handler. On expiry: `timeout` with `details: {action, timeout_s, started}` |
| `dry_run` | bool, optional | On mutating batch tools: validate only (D11) |

**Layer refs.** A UUID, a `/`-separated name path from the root, or a
bare unique name. The root is `"/"` (D4). Details (P1.6, 2026-11-24): in
names `\/` is a literal slash and `\\` a literal backslash; a leading
`/` on a path is optional (`/Face` means the top-level `Face` only); a
UUID-shaped ref is always read as an id; `"$key"` names a layer created
earlier in the same `create_layers` call.

**Paths.** Absolute, `~`-prefixed, or relative to the output dir. They
must resolve inside an allowed root (D7). Results always return the
resolved absolute path.

**Result envelope.** Tool-specific fields plus `summary` (one line) and
`elapsed_ms`. Errors raise `ToolError("<code>: <message>")` (D2).
Besides the plugin's D2 codes, the server's client
(`krita_mcp_server/client.py`) uses these for failures before a command
runs: `unavailable` (Krita not reachable), `unauthorized` (HTTP 401; the
message names the token file), `forbidden` (403 Host/browser check),
`too_large` (413 body size), `bad_request` (400/415), `not_found` (404
endpoint), `timeout` (no HTTP answer in time), `krita_error` (a reply that
isn't the expected JSON), and `protocol_mismatch` (the plugin's `/health`
`protocol` isn't the server's; checked once per process, before the first
command). `krita_health` never raises: when Krita can't be reached it
returns `{"running": false, "summary": …}`, otherwise the `/health` fields
plus `running: true` and `protocol_ok`.

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
Done in P1.7: every open document gets a handle (in `documents()`
order); `path` is the realpath of `fileName()`, or `null` if never saved;
`active` compares with `activeDocument()`, so headless it is always false.

### `krita_get_document_info(doc?)` · P0

Returns `doc`, `name`, `path`, `width`, `height`, `resolution` (dpi),
`color_model`, `color_depth`, `color_profile`, `modified`,
`layer_counts` (`{"paintlayer": 184, "grouplayer": 38, …}`, every type
present), `max_depth`, and `selection` (`null`, or the bounding rect of the
active selection).

Done in P1.7: `resolution` is `Document.resolution()`, a whole number
(Krita rounds: 72.5 dpi reads 73). `layer_counts` and `max_depth` come
from the tree walk, which skips the global selection mask; top-level
nodes are depth 1 and an empty document is depth 0. `selection` is
`{x, y, w, h}` from `Document.selection()`.

### `krita_new_canvas(width, height, name?, background?, color_profile?, resolution?)` · P0

- Limits: 16384 per side and 67.1 MP total (D8).
- `background`: a hex color, or `"transparent"`. `createDocument` already
  makes one opaque white paint layer named `Background` (spike Q4). That
  layer is reused, with no second layer added. It is filled with the
  color (skipped for opaque white), or, for `"transparent"`, replaced
  with an empty paint layer of the same name at the same place, which is
  cheaper than clearing 48 MP.
- `color_profile`: defaults to Krita's default sRGB profile for RGBA/U8
  (`sRGB-elle-V2-srgbtrc.icc`). Profiles that aren't installed fail with
  `bad_request`, and `details.available` lists the installed ones.
- Returns `doc`, `name`, `width`, `height`, `color_profile` and
  `background_layer` (its ID).
- The fill uses `bytes((b, g, r, 255)) * n`, about 0.2 s at 5800×8400.

Done in P1.7. The defaults are kept from before F001 for compatibility:
`width` 800, `height` 600, `name` `"New Canvas"`, `background`
`"#1a1a2e"`, `resolution` 120 dpi (1–10000). `background` takes any
opaque color Qt parses (`#rgb`, `#rrggbb`, SVG names); a translucent one
is `bad_request`. All parameters are checked before the document is
created. The result also has `resolution` (as `get_document_info`) and
`background` (normalized `#rrggbb`, or `"transparent"`). The new document
is not marked modified. In the GUI it opens in a view of the active window.

### `krita_open_file(path)` · P0

Returns `doc`, `path` (resolved), `name` (with the basename fallback),
`width`, `height`, and `layer_counts`. If the file is already open, the
existing handle comes back with `already_open: true` and the file is not
opened a second time.

Done in P1.7: `already_open` is in every result. "Already open" compares
the resolved path with the realpath of each open document's
`fileName()`. `openDocument` runs with the app-wide batch mode, so no
import or recovery dialog can block; Krita returning nothing (an
unreadable file) is `krita_error`. A missing file is `not_found`.

### `krita_save(doc?, path?, format?)` · P0

- No `path`: save in place (`Document.save()`). This fails with `conflict`
  if the document has never been saved.
- `format` defaults to the extension of `path`. `kra` uses `saveAs`, so
  the document's file path moves to the new location. Other formats use
  `exportImage` in batch mode, and the document's own path is unchanged.
- Returns `path` (resolved), `format`, `bytes` (file size),
  `doc_path_changed`, and `elapsed_ms`.
- Additive blend conversion (D17): `.psd` saves write `add` layers as
  `linear_dodge`, and `.ora` saves write `linear_dodge` as `add`, through a
  `clone()`. Converted layers are listed in `blend_conversions`.

Done in P1.7 (server signature `krita_save(path=None, doc=None,
format=None)`):

- `format` is lowercase without the dot, with `jpeg` read as `jpg`; one
  of the allowed image extensions. Given with a `path` whose extension
  differs, or with no `path` and a document file of another type, it is
  `bad_request`.
- Saving in place writes the document's own file, so that file must be
  inside an allowed root too (`forbidden_path` otherwise).
- Missing parent directories of `path` are created (inside the root;
  containment is checked again afterwards). Existing files are
  overwritten.
- `doc_path_changed` is true when `saveAs` moved the document to a new
  file. The result also has `doc`.
- Krita's `saveAs` and `save` return `True` without writing anything when
  the directory is missing, so the plugin also checks the file was
  written, and fails with `krita_error` otherwise (a `saveAs` that wrote
  nothing leaves the document marked modified).

### `krita_close_document(doc, discard=false)` · P1

Fails with `conflict` when the document is modified and `discard` is
false. With `discard`, the plugin calls `setModified(False)` then
`close()`. The handle is retired.

Done early, in P1.7 (so live tests can clean up). `doc` is required:
there is no active-document default, because closing the wrong document
would be bad (missing: `bad_request`). The `conflict` has
`details: {doc, name}`, and nothing is touched. `close()` returning
false is `krita_error`. Returns `doc` and `closed: true`; later calls
with the handle are `not_found`.

### `krita_set_canvas_size(doc, width, height, anchor="center")` · P2

`anchor` is one of `top-left`, `top`, `top-right`, `left`, `center`, `right`, `bottom-left`,
`bottom`, `bottom-right`. Uses `resizeImage(x, y, w, h)` with the offset
the anchor gives. Content is not scaled. The `summary` warns that a canvas
resize breaks existing rigs (CONVENTIONS: *Canvas*).

---

## Layer tree

### `krita_list_layers(doc?, root?, depth?, fields?)` · P0

- Returns `{"doc", "root", "layers": [<layer object>, …], "counts":
  {...}, "summary"}`. `layers` is a **flat list**, depth-first and top to
  bottom: the Layers docker's order, so a group comes right before its
  contents. Nesting is in each object's `parent` and `children` (ids, top
  to bottom), not in the list's shape.
- `root` is a layer ref, default `/`. With `/` the listed nodes are the
  root's descendants (the root isn't a layer). With a layer, it is listed
  **first**, followed by its subtree, so the caller sees the layer it
  asked for. Each object's `path` is still from the document root.
  The result's `root` is `"/"` or the layer's id. A name or
  path matching several layers is `ambiguous`.
- `depth` is unlimited by default; otherwise a whole number ≥ 1 (else
  `bad_request`). It counts levels below the root: `depth=1` lists the
  root's direct children (for a layer root, the layer and its direct
  children). Nodes at the cut-off still list their `children` ids, so the
  caller can go deeper with `root=<id>`.
- The document's global selection, a root-level `selectionmask` (spike
  Q9), is not listed. `get_document_info.selection` reports it.
- `fields` is an optional subset of layer-object keys. It keeps responses
  small for big documents. `id`, `name` and `children` are always
  included, keys keep the layer-object order, and only the requested keys
  are read from Krita. An unknown key is `bad_request` (`details.unknown`,
  `details.valid`).
- `counts` has nodes per type over the listed nodes (`{"grouplayer": 38,
  "paintlayer": 184, "total": 222}`).
- `bounds` is exact (spike Q2) and in canvas pixels, and is not clipped
  to the canvas: it may start at negative `x`/`y` or extend past the
  edge. `null` for a layer with no pixels. A group's bounds is the union
  of its children's.
- Settled in P2.1 (2026-11-25): `kritamcp/commands/layers.py`; one tree
  walk per call (about 7 ms headless for 229 nodes, all fields).

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
| `blend_mode` | string | `"normal"` | Checked against a fixed list of Krita composite-op IDs in the plugin (`kritamcp/blendmodes.py`). `setBlendingMode` stores any string, invalid ones included (spike Q15) |
| `locked`, `alpha_locked`, `inherit_alpha` | bool | `false` | |
| `pass_through` | bool | `false` | Groups only |
| `png` | path | — | Paint only. Read with QImage and converted to BGRA |
| `x`, `y` | int | `0` | Canvas offset of the PNG's top-left corner |

Order: specs are applied in list order, and each goes on top of its
parent's children unless `above` is given. So **listing siblings bottom
to top** reproduces a stack, and that is the order the manifest uses.

Returns `{"created": [{"key", "id", "name", "path"}], "summary": …}`,
plus `manifest`, `skipped` and `warnings` for a manifest (P2.4 notes
below). On failure, nothing stays created (D11).

Settled in P2.3 (2026-11-25), `kritamcp/commands/layers.py` (`specs`
only; `manifest` comes with P2.4):

- **Strict specs.** A spec must be an object; an unknown key is
  `bad_request` (`details.unknown`, `details.valid`), so typos fail. An
  explicit `null` counts as not given. On the server, `LayerSpec`
  (`krita_mcp_server/models.py`) forbids extra keys too and sends only the
  fields the caller set (`false` and `0` are sent).
- **Field rules.** `name`: non-empty, at most 255 characters, no control
  characters. `key`: non-empty, at most 255 characters, no `$` or `/`,
  unique in the call. Bools must be JSON booleans (not `0`/`1`).
  `opacity`: a finite number in 0–1. `blend_mode`: one of the 138 IDs in
  `kritamcp/blendmodes.py` (Krita's registered blending modes; an error
  suggests close matches). `pass_through` only on groups, `png` only on
  paint layers, `x`/`y` (whole numbers within ±163840) only with `png`.
  The shared property checks live in `kritamcp/layerprops.py`, for
  `update_layers` too.
- **`$key` and placeholders.** During validation a keyed spec stands for
  a placeholder of the layer it will create. `parent` and `above` may name
  it as `"$key"` only after the spec that defines it (a later or unknown
  key, or a spec naming itself, is `not_found`). Layers made in this call
  are reachable only by `$key`, not by name or path. `parent` must be `/`,
  an existing group or a planned group (`bad_request` for anything else).
  `above` must be an existing child of that parent or a planned spec with
  the same parent (`bad_request` otherwise). While applying, placeholders
  resolve to the ids just created.
- **PNGs.** `png` goes through `resolve_allowed_path` (`forbidden_path`,
  `not_found`), then its header is read: not a PNG or a corrupt IHDR is
  `bad_request`, a side over `max_canvas_dim` or more than
  `max_canvas_pixels` is `too_large`. Any `png` needs an 8-bit RGBA
  document (`conflict` naming its color model and depth, D5). Only the
  header is read up front, so a file corrupt past its header fails while
  applying (`bad_request`, rolled back). Decoding is `QImage` →
  `Format_ARGB32` (`kritamcp/pixels.py`); pixels outside the canvas are
  kept.
- **Apply.** Per spec: decode the PNG, `createNode` / `createGroupLayer`,
  `addChildNode(node, above or None)`, properties, `setPixelData`, and
  `locked` last. If a step fails after the node exists, that spec removes
  its own node before the batch rolls back the earlier ones (newest first).
  Each inverse removes its node by id from the live document passed at
  call time. A successful call refreshes the projection and pushes one
  journal entry.
- **Result.** `{doc, dry_run: false, created: [{key, id, name, type,
  path}], counts}`, where `counts` is per Krita type plus `total` and
  paths come from a fresh walk. With `dry_run`: `{doc, dry_run: true,
  would_create: [{key, name, type, parent, above, png?, x?, y?, w?, h?}],
  counts}`. Here `parent`/`above` are `"/"`, an id or `"$key"`, and
  nothing changes.

Settled in P2.4 (2026-11-25), `manifest=…` (`commands/layers.py`
`_load_manifest`, `kritamcp/manifest.py`):

- **Exactly one input.** Both `specs` and `manifest`, or neither, is
  `bad_request` (the server's wrapper checks this too, before calling
  Krita).
- **Loading.** `manifest` goes through `resolve_allowed_path(...,
  allow_json=True, must_exist=True)`: outside the roots or a disallowed
  extension is `forbidden_path`, a missing file `not_found`; an image
  extension is `bad_request` (it must be `.json`). Then `manifest.read`
  and `manifest.to_specs(m, dirname(resolved path))`, then the same
  validate / apply path as `specs`. Each `png`, joined onto the manifest's
  directory, still goes through `resolve_allowed_path`, so a `..` path
  that leaves the roots is `forbidden_path`. A manifest whose entries are
  all skipped is `bad_request`. `x` / `y` on an entry with `png: null` are
  dropped (informative; `ora_to_manifest` writes them).
- **Errors name the manifest entry.** Projection entries are skipped, so
  spec positions shift; every error is mapped back to the manifest's
  `layers[i]`: the message starts `layers[i]: ` (mid-apply:
  `create_layers: layers[i] failed: …`), `details.index` is `i` and
  `details.field` is `"layers"` (`batch.validate_all` / `batch.run`
  `positions=`). Structure errors from `to_specs` have the same shape;
  `details.param` names the entry's key at fault (`parent`, `png`, …),
  as in spec validation errors.
- **Result.** As for `specs`, plus `manifest` (the resolved path),
  `skipped` (`[{index, key, reason}]`) and `warnings`. One warning so far:
  `{code: "document_size", message, manifest_size: {w, h}, doc_size: {w,
  h}}` when the manifest's `document.width/height` (whole numbers) differ
  from the target document's. It is not an error: layers keep their
  manifest offsets. `dry_run` works the same way and returns these too.
- **Mao (acceptance 1, headless,** `tests/krita/test_mao_headless.py`**).**
  One call creates the 184 paint layers and 38 groups in about 8 s on a
  5800 × 8400 canvas (2026-11-25).

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
- *(P2.4, 2026-11-25; `kritamcp/manifest.py`)* Structure rules
  `create_layers(manifest=…)` enforces, all `bad_request` with
  `details.index` (and `details.field: "layers"`, `details.param` the
  entry key at fault): `version` must be 1; top-level keys other than
  `version`/`generator`/`document`/`layers` and entry keys other than the
  spec fields plus `id`, `path`, `stack_index`, `w`, `h`, `projection` are
  rejected (the informative ones are dropped). Every entry needs a unique
  `key`. `parent` is `"/"` or `"$key"` of an earlier group entry, and
  `above` is `"$key"` of an earlier sibling: names and UUIDs are not
  accepted in a manifest. `projection: true` entries are skipped (reported
  in `skipped`) and may not be a `parent` or `above` target. `png` is a
  relative path or `null`; absolute paths are rejected. With `png: null`,
  `x` / `y` are dropped. At most 2000
  layers (`too_large`), and the file is capped at 16 MB.

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
| `blend-mode` | error | A paint layer's blend mode is not `normal`/`multiply`/`add`/`linear_dodge`. Groups are exempt, since group blending doesn't matter to the rig. `add` and `linear_dodge` render identically and both count as the rig's Add (D17). If the config sets `lint.additive_mode` to one spelling, the other is a finding with a fix hint |
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
| `krita_health` | Adds `protocol`, `version`, `krita_version` (done in P1.3, with `running` and `protocol_ok` added server-side), then `allowed_dirs` (resolved roots, the output dir first), `config_file`, `config_loaded`, `limits` and `config_errors` (done in P1.4; the summary counts the dirs and lists the errors), then `busy` (`{"action", "elapsed_ms"}` or `null`) and `queued` (done in P1.5; the summary adds e.g. "busy: save for 12.3 s, 1 queued") |
| `krita_new_canvas`, `krita_open_file`, `krita_save` | As above. Done in P1.6: `new_canvas` and `open_file` return `doc` (the new document's handle; the registry keeps the wrapper, so a headless `new_canvas` leaves an open document) and `save` takes `doc`. The rest done in P1.7 |
| `krita_get_canvas` | Takes `doc` (done in P1.6). Becomes a shortcut for `render` into the output dir |
| `krita_set_color` | Stored plugin-side (D15) and also set on the active view when there is one; works without a view. Returns `color` (normalized `#rrggbb`) and `view_updated`. An invalid color is `bad_request`. Painting uses the stored color, else the view's foreground, else black. **No `doc`**: the color is plugin-global state that applies to every document, which is what lets painting work in a document with no view (D15). Done in P1.6 |
| `krita_set_brush` | `size` and `opacity` are stored plugin-side (the stroke defaults) and `size` is also set on the view if there is one; a `preset` needs a view (`conflict` without one, and nothing is stored). Returns the stored `preset` (matched name or null), `size`, `opacity` and `view_updated`. No `doc`, like `set_color`. Done in P1.6 |
| `krita_list_brushes` | Structured results only |
| `krita_stroke`, `krita_fill`, `krita_draw_shape`, `krita_clear` | Accept `doc`, `layer`. `layer` defaults to the document's active node; anything but a paint layer (including an active group or mask) is `conflict` with `details: {doc, layer, type}`. Headless there is never an active node, so `layer` is required there (`not_found` otherwise). No view needed. Results include `doc` and `layer` (id). `clear` takes `"transparent"`, which zeroes the layer over the union of its bounds and the canvas; a color fills the canvas rect. Done in P1.6 |
| `krita_get_color_at` | Accepts `doc`, `layer` (sample one layer instead of the composite): a paint layer gives its own pixel, a group its projection, other types `conflict`. `0 <= x < width`, `0 <= y < height` of the document, else `bad_request` naming the size. Results include `doc` and `layer` (id, or null for the composite). Done in P1.6 |
| `krita_undo` / `krita_redo` | Become the agent journal (D6). The UI-stack triggers move to `krita_ui_undo` / `krita_ui_redo`. Until then (P5.1) they take no `doc`: they act on the active view's document |
