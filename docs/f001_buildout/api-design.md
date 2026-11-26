# F001 API design

The contract for every tool after the buildout. The rationale is in
[decisions.md](decisions.md), and the request as received is in
[spec.md](spec.md).

## Conventions shared by all tools

**Names.** MCP tool `krita_<name>`; plugin action `<name>` (D1).

**Common parameters.**

| Param | Type | Meaning |
|---|---|---|
| `doc` | string, optional | Document handle (`doc-3`), or the absolute path of an open document. Defaults to the active document (D3). Every tool that acts on one document takes it; tools that act on app or UI state (`health`, `set_color`, `set_brush`, `list_brushes`) don't. `ui_undo`/`ui_redo` take it to pick the view they act on (P5.1, D6). Details (P1.6, 2026-11-24): a path (absolute or `~`) is compared after `realpath` with each open document's `fileName()`; it only identifies an open document, so it isn't checked against the allowed roots. Several matches are `ambiguous` (`details.matches` lists their handles). With no `doc` and no active document (always so headless), `not_found`. Results of document tools include `doc` |
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

Additive blend conversion done in P2.7 (`kritamcp/blendexport.py`):

- The table is `psd`: `add` → `linear_dodge`; `ora`: `linear_dodge` →
  `add`. Every node of the tree is checked, groups (PSD keeps a group's
  blend mode: `lddg` reopens as `linear_dodge`), locked layers and masks
  included; the root-level global selection is skipped. Other formats
  (`kra`, `png`, `jpg`, …) are never converted.
- Nothing to convert: the document is exported as before and no clone is
  made. Otherwise `doc.clone()`, the modes set on the clone's nodes (found
  by UUID), `refreshProjection()` + `waitForDone()`, `exportImage` of the
  clone in batch mode, and the clone closed in `finally`. The open
  document's modes, `fileName()` and `modified` are unchanged. The clone
  is not in `Krita.instance().documents()` (headless, verified), so no
  handle is ever issued for it.
- **In place:** a document whose own file is `.psd` or `.ora` and that
  has layers to convert is saved by exporting the converted clone over
  that file instead of `Document.save()`, then `setModified(False)` on the
  document: its content is saved, but the file holds the converted
  spelling, so reopening it gives `linear_dodge` (PSD) or `add` (ORA)
  where the open document has the other. Without layers to convert it is
  `Document.save()` as before.
- `blend_conversions`: `[{id, path, from, to}]` in the Layers docker's
  order (`path` as in the layer object), `[]` when nothing was converted.
  The server's summary adds e.g. "; 2 Add layers written as Linear Dodge
  for PSD".

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

Settled in P2.5 (2026-11-25), `kritamcp/commands/layers.py`:

- **Strict changes**, as `create_layers` specs: an object, unknown keys
  `bad_request` (`details.unknown`, `details.valid`), an explicit `null`
  is not given. `id` is a layer ref (not `/`, not `$key`). At least one
  property is needed. The same values rules (`kritamcp/layerprops.py`):
  `pass_through` only on groups (`details.param`). The same layer named by
  two changes is `bad_request` (`details.first`: the earlier change), since
  the intended result is ambiguous.
- **Apply.** Per change: read the old values of exactly the given fields,
  then set them; an unlock goes first and a lock last. The inverse sets the
  old values back (opacity as Krita's exact 0–255 value). Refresh, one
  journal entry, default timeout 300 s.
- **Warnings.** `{code: "duplicate_name", message, id, name, others}` for
  each rename whose new name another layer has after the whole call
  (`others`: their ids, stacking order). Renames that swap names, or
  rename a layer to its own name, don't warn.
- **Result.** `{doc, dry_run: false, updated: [{id, path, changed:
  [fields set]}], warnings}`, paths after the call. With `dry_run`:
  `{doc, dry_run: true, would_update: [{id, path, set: {field: value}}],
  warnings}`.

### `krita_move_layers(doc?, moves, dry_run?)` · P0

Each move is `{id, parent?, above? | below? | index?}`, with at most one
position field. `parent` defaults to the current parent. With no position
field, the layer goes on top of the new parent. Moving a group into its own
subtree fails with `bad_request`. Implemented as `removeChildNode` +
`addChildNode`. Bounds are unchanged, since moves never shift pixels.

Settled in P2.5 (2026-11-25), `kritamcp/commands/layers.py`:

- **Fields.** Strict objects as above (`MOVE_KEYS`). Two position fields
  are `bad_request` (`details.conflicting`). `parent` is `/` or a group
  (a paint layer or mask is `bad_request`). `above` / `below` must be a
  child of the target parent other than the moving layer; pass `parent`
  to move into another group. `index` counts from the bottom (0) among the
  target parent's children **without the moving layer**, `0 ≤ index ≤`
  their count (the count = on top); not a whole number or out of range is
  `bad_request` (`details.max`). Same counting as the layer object's
  `index` (the root's global selection mask is never counted).
- **Planned state.** Refs are resolved against the document as it was
  before the call, but each move is validated against a simulated tree
  (parent and children ids) that the earlier moves of the call have
  already changed: a move into the layer's own subtree, or next to a
  sibling that an earlier move took away, fails with `details.index` of
  that move, even if it would be valid in the starting tree. The same
  layer may move several times in one call.
- **Apply.** Per move: `removeChildNode`, then `addChildNode` at the
  planned place. Removing a node detaches its whole subtree in Krita
  (krita-api-notes § Nodes), so the subtree is captured first and
  re-added after; groups and layers with masks move complete, with their
  ids, pixels, bounds and properties. The bottom place is reached by
  inserting above the bottom child and then moving that child above the
  layer. A move to where the layer already is does nothing and records no
  inverse. If an insert fails, that move puts the layer back first, then
  the batch rolls back.
- **Inverse.** Move the layer back under its old parent, directly above
  the sibling that was directly below it (by id, at call time), or to the
  bottom; if that sibling is gone, at the old index (clamped). Rollback
  runs newest first, so chained moves (also of the same layer) restore
  exactly.
- **Result.** `{doc, dry_run: false, moved: [{id, path, parent,
  index}]}`, one per move in order, each the layer's place after the
  whole call (`parent` is `"/"` or a group id, `index` from the bottom).
  `dry_run` returns `would_move` with the same fields, from the simulated
  tree (identical to what the real call reports). Default timeout 300 s.

### `krita_delete_layers(doc?, ids, dry_run?)` · P0

Deleting a group deletes its subtree. Returns the deleted IDs, with
subtree members included.

Settled in P2.5 (2026-11-25), `kritamcp/commands/layers.py`:

- **Refs.** `ids` is a list of 1–2000 layer refs; `/` is `bad_request`,
  an unknown ref `not_found` (`details.index`). Duplicates and refs inside
  another listed layer's subtree are folded in.
- **Apply.** The remaining layers are deleted top first with
  `Node.remove()`. The inverse keeps the detached node and its captured
  subtree (D6, Q18) and re-inserts them under the old parent, directly
  above the sibling that was below it (by id at call time; the bottom if
  there was none; the old index if that sibling is gone). It exposes
  `node_ids` (the subtree, top to bottom) and `nbytes` (about `w*h*4` per
  paint layer, from bounds) for the journal's caps (P5.1). A mid-apply
  error's `details.index` is the layer's position in `ids`.
- **Result.** `{doc, dry_run: false, deleted: [ids, subtree members
  included, top to bottom], count, warnings}`; `dry_run` returns
  `would_delete` and `count`. A delete whose `nbytes` is over the
  journal's memory cap still happens, but can't be undone (detached
  layers can't be spilled to disk, P5.1): `warnings` has
  `{code: "not_undoable", nbytes}` and the journal is cleared (D11).
  Deleting the active layer is fine (headless `activeNode()` is always
  `None`). Default timeout 300 s.

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

### `krita_export_layers(doc?, ids="all", dir, crop=true, include_hidden=true, include_groups=false, overwrite=false, png_level=null)` · P0

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

*As built (P3.2, 2026-11-25; `kritamcp/commands/pixels.py`, 300 s,
non-mutating):*

- **Validation**, before anything is written: `dir` is required and goes
  through `resolve_output_dir(dir, overwrite=…)` last (D7: inside a root,
  created if missing, empty unless `overwrite`; `forbidden_path`,
  `conflict` for a file or a non-empty directory); the document must be
  RGBA/U8 (`conflict`, D5); `crop`, `include_hidden`, `include_groups`,
  `overwrite` are booleans (`null` = default); `png_level` is 0–9 or
  `null` (`pixels.save_png(level=)`, as `render`); `ids` is `"all"` or a
  non-empty list of refs (`resolve_many`: `not_found` / `ambiguous` with
  `details.index`; `"/"` in the list means everything).
- **Subset rule.** A listed node brings its whole subtree. Every ancestor
  group of a listed node is written too, as a plain group entry (no
  projection, none of its other children), so `parent` links and nesting
  survive and the manifest still rebuilds. Only listed groups and groups
  inside them get projections.
- **Order and keys.** One `nodes.tree` walk; entries in create order
  (depth-first, siblings bottom to top, a parent before its children).
  The entry at 1-based position `n` has key `L<n:04d>`, PNG
  `<n:04d>_<slug(name)>.png` (`manifest.slug`, the same as
  `tools/ora_to_manifest.py`; unique since `n` is) and `stack_index`
  `n - 1`. So `stack_index` is the entry's position in `layers`, as in
  `ora_to_manifest`: 0 draws first; a group's entry comes before its
  contents and its projection entry right after them. `parent` is `"/"` or
  `"$<key>"`; `path` is escaped like refs (`Eye\/L`).
- **Entries.** Paint: `key, id, name, path, type: "paint", parent, visible,
  opacity, blend_mode, inherit_alpha, alpha_locked, locked, png, x, y, w,
  h, stack_index`. Group: `key, id, name, path, type: "group", parent,
  visible, opacity, blend_mode, inherit_alpha, locked, pass_through,
  stack_index`. `opacity` is `round(v / 255, 4)` (`layerprops.from_krita_opacity`,
  as `list_layers`), and `round(opacity * 255)` gives Krita's value back
  for all 256 values. `blend_mode` is as stored (no D17 conversion here).
- **Paint pixels.** `pixelData(bounds)` (exact bounds, unclipped), then
  `opaque_bounds` (alpha ≥ 1) of that window. With `crop`, the PNG is that
  rectangle, possibly off the canvas, written at its offset; without, a
  `pixelData(0, 0, W, H)` canvas-sized PNG at (0, 0), and a layer whose
  opaque bounds reach past the canvas is listed in warning `off_canvas`
  (those pixels are lost). No pixel with alpha > 0: `png: null` and no
  `x/y/w/h` (also with `crop=false`). The PNG holds the layer's own
  pixels, bit for bit (opacity not baked in, unlike `Node.save`, D5).
- **Hidden.** By default hidden nodes are exported with `visible: false`.
  With `include_hidden=false` a hidden node is left out (`skipped`,
  reason `hidden`) and everything under it too (reason `inside_hidden`).
- **Group projections** (`include_groups`): a separate entry after the
  group's contents, `projection: true`, `type: "paint"`, its own key,
  `parent` the group's parent, and the group's `id`, `name`, `path`,
  `visible`, `opacity`, `blend_mode`. `create_layers` skips it, and
  nothing names it as `parent` or `above`. The PNG is the group composited
  on its own: `projectionPixelData`, which includes clipping inside the
  group and excludes the group's own opacity. Krita keeps no projection
  of its own for a **pass-through** group (it reads empty) or a current
  one for a **hidden** group or anything inside one (empty or stale), so
  those are read from a `clone()` (D10; closed in `finally`, the user's
  document untouched) in which they are made isolated and visible (and
  their ancestors visible): one clone and one refresh per nesting depth,
  since groups at one depth are disjoint subtrees. A projection with no
  alpha > 0 has `png: null`. So for a pass-through group the PNG is its
  isolated composite, not its effect in the document.
- **Not exported**, listed in `skipped` as `{id, path, type, reason}`:
  masks (reason `mask`; `transparencymask`, `filtermask`,
  `transformmask`, `colorizemask`, `selectionmask`) and filter, fill,
  clone, vector and file layers (reason `unsupported_type`). A paint
  layer with masks is still exported, its own pixels without the masks
  applied. Warnings: `masks_not_applied` (exported layers with a visible
  mask) and `not_exported` (other layer types), both with `paths`: a
  rebuild will not render the same.
- **Other warnings.** `not_recreatable` (`layers: [{path, problem}]`): an
  entry `create_layers` would refuse as it is, a name it rejects
  (empty, control characters, over 255) or a blend mode not in
  `blendmodes.py` (Krita stores any string). `too_many_layers`: more than
  2000 creatable entries (`create_layers`' cap).
- **Writing.** Each PNG via `pixels.save_png` (temp file + rename);
  `manifest.json` last via `manifest.write`, with `document: {name,
  width, height, resolution, color_model, color_depth, color_profile}`.
  On a failure the PNGs this call created are removed (files it replaced
  under `overwrite` stay replaced) and no manifest is written.
- **Result.** `{doc, dir, manifest (path), count (PNGs written), layers
  (the manifest entries), skipped, warnings, bytes (PNGs + manifest)}`.
- **Round trip** (acceptance § 2, headless, `tests/krita/test_export_layers.py`):
  a synthetic document (nested groups, duplicate names, a hidden group, an
  empty layer, partial alpha, an off-origin layer, one past the canvas,
  multiply / add / linear_dodge / screen, inherit_alpha, a pass-through
  group, opacity 128 and 200, locks) exported and rebuilt on a new
  transparent canvas renders **byte-identical** (`doc.pixelData` of the
  canvas), with matching trees (paths, types, visibility, opacity, blend
  modes, locks, clipping, pass-through, bounds); also with `crop=false`
  plus `include_groups`. A transparency mask or a filter layer makes it
  differ, as warned.
- **Colour under zero alpha** is not always carried over. Krita's
  projection keeps colour at fully transparent pixels from layers whose
  data has coloured alpha-0 pixels (e.g. a PNG's transparent margin);
  `crop` cuts to alpha > 0, so the rebuild has (0, 0, 0, 0) there. Every
  pixel with alpha > 0, and alpha everywhere, still matches. `crop=false`
  keeps those bytes within the canvas (headless test).
  **Mao** (`tests/krita/test_mao_headless.py`, 2026-11-25): export (crop,
  `png_level` 1) of the rebuilt Mao 5.6 s, 184 PNGs, 54.7 MB; Qt's
  default level 13.2 s, 42.5 MB; with `include_groups` (38 pass-through
  groups via clones) 10.2 s, 222 PNGs, 88.5 MB. Rebuilding the export:
  same tree, bounds included; projections differ only in 11 alpha-0
  pixels' colour (rows 4728–5151, e.g. Thumb R's coloured 1-px margin at
  x = 1524).

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

### `krita_import_png(doc?, id, png, x=0, y=0, mode="replace", clear_outside=false, dry_run?)` · P0

- The target must be a paint layer, otherwise the call fails with `conflict`.
- `replace`: the PNG's rectangle overwrites the layer's pixels exactly,
  alpha included. Pixels outside the rectangle are untouched.
- `over`: source-over compositing onto the existing pixels within the
  rectangle (`QPainter`, `CompositionMode_SourceOver`).
- `clear_outside: true` (with `replace`): first clear the layer's whole
  extent, so the layer then contains only the PNG.
- Returns the changed rectangle and the layer's new bounds.

*As built (P3.3, 2026-11-25; `kritamcp/commands/pixels.py`, 300 s,
mutating):*

- Validation, all before any change: `id` is any layer ref (`"/"` is
  `bad_request`); a non-paint target is `conflict` with `details.type`
  (`grouplayer`, `transparencymask`, `filterlayer`, …); a document that
  isn't RGBA/U8 is `conflict` with `details.color_model` / `color_depth`
  (D5); `png` goes through the allowed roots (`forbidden_path`,
  `not_found`), must be a PNG by its header (`bad_request`) and within the
  canvas size limits (`too_large`); `x`, `y` are whole numbers within
  ±163840 (a `null` means 0); `mode` is `replace` or `over`;
  `clear_outside` with `over` is `bad_request`.
- `x`, `y` are where the PNG's top-left pixel lands. The rectangle may lie
  partly or wholly off the canvas: Krita keeps those pixels and `bounds`
  extends past the edge.
- `over` is `pixels.composite_over`: Qt source-over on a 16-bit
  premultiplied working image (`Format_RGBA64_Premultiplied`; 8-bit
  premultiplication was up to 51 levels off at low alpha), measured within
  one level of the exact formula at every alpha. It is exact where the
  PNG pixel is opaque, where the layer pixel is fully transparent (the PNG
  pixel), and where the PNG pixel is fully transparent (the layer pixel is
  kept bit for bit, colour under zero alpha included).
- `clear_outside` zeroes the layer over its old `bounds()` only, then
  writes the PNG; with an empty layer it is the same as `replace`.
- Result: `{doc, layer (id), png (resolved path), mode, clear_outside,
  dry_run: false, changed: {x, y, w, h}, bounds: {x, y, w, h} | null,
  warnings}`. `changed` is the PNG's rectangle, or with `clear_outside`
  the bounding rectangle of it and the old bounds. `bounds` is the layer's
  new exact bounds. With `dry_run`: `would_change` instead of `changed`,
  no `bounds`, nothing changed and no journal entry.
- Undo (D6): before writing, the old pixels of the affected rectangles
  are read into the inverse, `RestorePixels(node_id, patches)` with
  `node_ids` and `nbytes`; the patches are the PNG's rectangle, plus with
  `clear_outside` the old bounds (one bounding rectangle when that is no
  bigger than the two apart). The inverse writes them back on the layer
  looked up by id in the live document at call time. One journal entry
  per call (`batch.run` with one plan; a failure mid-write restores the
  old pixels and reports `details.applied` / `rolled_back` like the other
  batch tools). Over the journal's memory cap the old pixels are spilled
  to a temp file (P5.1, D6); an entry over the disk cap too is not
  stored, the journal is cleared and the result warns `not_undoable`
  with `nbytes`,
  as `delete_layers` does.

### `krita_render(doc?, path, region?, scale=1.0, only?, hide?, background?, png_level?)` · P0

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
  *(P3.1, 2026-11-25)* `pixels.save_png(…, level=)` maps the level to
  `QImage.save`'s quality as `100 - ceil(91 * level / 9)`, the inverse of
  Qt 5.15's `qpnghandler.cpp` (`compression = (100 - quality) * 9 / 91`).
  `null` passes quality -1: Qt sets no level and libpng uses zlib's
  default, 6. Writes go to a temp file in the same directory and are
  renamed into place, so a failed write leaves no partial file.
- Returns `path`, `width`, `height` and `elapsed_ms`.
- `krita_get_canvas(filename)` stays as a shortcut for
  `render(path=<output dir>/<filename>)`.

As built (P3.4, 2026-11-25; `commands/render.py`, pure parts in
`renderplan.py`):

- **Order of checks**, all before anything is read or written: `path`,
  `doc`, `region`, `scale` and the output size, `background`,
  `png_level`, then `only` / `hide`. Missing parent directories of
  `path` are created (inside the allowed root) only then.
- **`path`** goes through `resolve_allowed_path` and must end in `.png`
  (any case); another allowed image type (`.jpg`, …) is `bad_request`,
  a type that isn't allowed at all `forbidden_path`. An existing file is
  replaced (atomically, `save_png`).
- **`region`**: exactly the keys `x, y, w, h`, whole numbers (not bools)
  within ±`COORD`; `w`, `h` ≥ 1. It is clipped to the canvas; a region
  that lies entirely off it is `bad_request`. The result's `region` is the
  clipped rectangle.
- **`scale`**: a number, 0 < scale ≤ 4. Each output side is
  `floor(side * scale + 0.5)` (nearest, halves up), at least 1. The
  output's pixel count must be ≤ `max_canvas_pixels`, else `too_large`
  (`details: {limit, max, pixels, w, h}`); the per-side limit isn't
  applied to renders. When the output size equals the region's (scale 1,
  or a scale that rounds back to it) nothing is resampled and the PNG holds
  the projection's bytes exactly, colour under zero alpha included.
- **Resampling** scales a `Format_RGBA64_Premultiplied` copy. Qt's own
  path for `Format_ARGB32` premultiplies at 8 bits, which loses colour as
  alpha drops: halving a test image, colours were off from the exact
  2×2 average by up to 6.6–7.4 levels at alpha ≥ 32 and 171–182 at alpha
  1–31; at 16 bits, by 0.5 and 1.3 (alpha within 0.5 either way). It costs
  about 5–10 ms more at Mao size (48 ms for a 0.25 scale of a 5800×8400
  image). Premultiplied scaling never bleeds the colour of alpha-0 pixels
  into visible ones, and resampled output has (0, 0, 0, 0) wherever alpha
  is 0.
- **`only` / `hide`**: non-empty lists of refs (`resolve_many`:
  `not_found` / `ambiguous` with `details.index`; `"/"` and batch keys
  are `bad_request`); repeats are dropped. The visibility to set
  (`renderplan.visibility_changes`): each `only` node and its ancestors
  visible; the siblings of those nodes and of their ancestors hidden,
  except inside an `only` subtree; nodes below a hidden node left as they
  are (its parent hides them); then every `hide` node hidden (hide wins).
  So a hidden child of an `only` group stays hidden, and an `only` layer
  inside another `only` group doesn't hide that group's other contents.
  Only the nodes whose visibility differs are changed. If none differs
  (e.g. `hide` of a hidden layer), the projection is read directly.
- **The clone (D10 B)**: `doc.clone()`, `setVisible` on its nodes found by
  UUID (in a helper's frame, so no node wrapper outlives it),
  `refreshProjection()` + `waitForDone()`, `projection(region)`, then
  `setModified(False)` + `close()` in `finally`; the clone is never
  touched after that. The QImage is Krita's own copy and stays valid. The
  headless suite checks that a clone's projection follows visibility
  changes made in it, and that after subset renders the original's
  projection, every node's visibility, `modified()` and the number of open
  documents are unchanged.
- **Direct reads** call `waitForDone()` only (pending updates), no
  `refreshProjection()`: the projection read right after `open_file` of
  the 222-node Mao `.kra` equalled a refreshed one byte for byte.
- **`background`**: `null` or `"transparent"` for none; otherwise any
  colour `QColor` parses (`#rgb`, `#rrggbb`, `#aarrggbb`, SVG names),
  translucent allowed; anything else is `bad_request`. It is composited
  under the (scaled) image with `pixels.composite_over` (16-bit; exact
  where the image is opaque or transparent). Echoed as `#rrggbb`, or
  `#aarrggbb` when translucent.
- **Colour spaces**: `Document.projection` is Krita's `convertToQImage`
  with the default profile, i.e. 8-bit sRGB `Format_ARGB32`
  (non-premultiplied). For RGBA/U8 in an sRGB profile (`sRGB-elle-V2-srgbtrc`,
  `sRGB built-in`) that is exactly `Document.pixelData`; every other depth,
  model and profile (U16, F16, F32, GRAYA, CMYKA, ClayRGB, …) is converted
  to 8-bit sRGB by Krita. So `render` works on any document; it does not
  call `require_rgba8`.
- **Result**: `doc`, `path` (resolved), `width`, `height` (the PNG),
  `region` (clipped), `scale`, `bytes` (file size), and, when given, `only`
  / `hide` (resolved ids), `background` (normalized), `png_level`; plus
  `elapsed_ms` in the envelope. Non-mutating, 300 s default timeout.
- **Server** (`krita_render`): `region` is a pydantic `Rect`
  (`extra="forbid"`, strict ints, `w`/`h` ≥ 1); `scale` is checked
  (0, 4] and `png_level` 0–9 before the call; `scale` is sent only when it
  isn't 1. The summary gives the size, scale / region, `only` / `hide`
  counts, background and file size, and for a full-size render of 4 MP or
  more the cost hint (scale 0.25 or `png_level=1`).
- **Timings** (Mao, 5800×8400, headless): full render 4.0 s at the default
  level (11.3 MB), 1.6 s at `png_level=1`, 0.37 s at scale 0.25 (1.3 MB).
  A refresh of the reopened Mao `.kra` took 0.07 s.
- **`get_canvas(filename)`**: the base name of `filename` (any directory
  part dropped) with `.png` appended unless it ends in `.png` (any case),
  in the output dir; then a full-canvas `render` (same code: projection →
  QImage → `save_png`, no longer `exportImage`). Result: `doc`, `path`
  plus `render`'s fields.

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

Details (P4.2, 2026-11-25), `kritamcp/lint.py`: a finding is
`{rule, severity, layer_id, path, message, related_ids?}`; document-level
findings have `path: null` too and sort first; ties at one layer sort by
rule id. `rules` must be a non-empty list; an unknown name is
`bad_request` whose message lists the valid sets and rules (also in
`details.sets` / `details.rules`, with `details.unknown`). Naming a rule
twice or through a set runs it once; `rules_run` lists the rules run, in
registry order (the order of the tables below). `severity_min` (`info`,
`warning`, `error`; anything else is `bad_request`) drops less severe
findings, and `counts` counts the findings returned.

As built (P4.4, 2026-11-25): the plugin command `lint` (300 s,
read-only) in `commands/analysis.py` checks `rules` and `severity_min`
first (`lint.check_args`, so a bad value fails before the document is
even resolved), resolves `doc`, collects a snapshot with the config's
`lint` settings and runs `lint.run`. The result:

```json
{"doc": "doc-2", "findings": [...], "counts": {"error": 3, "warning": 1, "info": 0},
 "rules_run": ["node-type", "layer-style", ...],
 "settings": {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4,
              "stray_speck_max_px": 64},
 "pixel_stats": true}
```

`settings` are the ones used; `pixel_stats` is false for a document that
isn't 8-bit RGBA (no alpha statistics, so `empty-layer`, `out-of-canvas`
and `stray-alpha` can't fire; `color-space` reports it). `krita_lint(doc,
rules, severity_min, timeout_s)` validates `severity_min` against
`info`/`warning`/`error` in its schema, refuses an empty or string
`rules` itself, and adds the summary: `"3 errors, 1 warning in doc-2 (10
rules)"`, `"No findings in doc-2 (10 rules)"`, plus "pixel rules skipped"
when `pixel_stats` is false. `/health` reports the `lint` settings next
to `limits`. `lint_snapshot` stays a plugin command without a tool.

#### Snapshot (collector)

The rules run over one snapshot, collected in Krita by
`commands/analysis.py` `collect_snapshot(doc, settings)` (P4.1). The
debug command `lint_snapshot(doc?)` (300 s, read-only, no MCP tool)
returns `{"doc", "snapshot"}`.

```json
{"document": {"width": 64, "height": 64, "color_model": "RGBA", "color_depth": "U8",
              "color_profile": "sRGB-elle-V2-srgbtrc.icc", "pixel_stats": true},
 "settings": {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4,
              "stray_speck_max_px": 64},
 "nodes": [{"id": "{…}", "name": "Blur", "path": "Blur", "type": "filterlayer",
            "parent": null, "depth": 1, "order": 0, "visible": true, "opacity": 1.0,
            "blend_mode": "copy", "inherit_alpha": false, "alpha_locked": false,
            "locked": false, "pass_through": null, "has_layer_style": false,
            "bounds": {"x": 0, "y": 0, "w": 64, "h": 64}, "alpha": null}]}
```

- `nodes`: every node but the root-level selection mask, in global
  stacking order (depth-first, top to bottom, the Layers docker's order);
  `order` is the list index, `depth` 1 is top level, masks are nodes with
  their layer as `parent`. Values as in the layer object.
- `settings`: the config's `lint` section over the defaults shown;
  invalid values fall back to the default.
- `alpha` (paint layers only, else null): `pixel_count` (`alpha > 0`),
  `opaque_bounds` (tight rectangle around `alpha > 0`, canvas coordinates,
  may reach past the canvas), `stray_count`. Read with `pixelData` over the
  layer's `bounds()`, which is exact and unclipped, so off-canvas pixels
  count. Only for 8-bit RGBA: on another document `document.pixel_stats`
  is false and every `alpha` is null (a non-RGBA/U8 layer in an RGBA/U8
  document also gets null).
- `stray_count` (`alphastats.py`): weak = `0 < alpha < below`, strong =
  `alpha ≥ below`, both masks by `bytes.translate`. Stray = weak and not
  in the strong mask dilated by `distance`; counted as
  `(weak & ~grown).bit_count()` over big ints, no per-pixel loop. The
  dilation is `Selection.grow(d, d)` on a standalone `Selection`
  (never the document's), whose kernel is a little fuller than the
  Euclidean disc: the axis points at distance `d` are covered, and at
  `d = 4` so are (3, 3) and (4, 2), but not (4, 3)
  (`alphastats.kernel_half_widths`). If `grow` is missing or fails a
  self-check, `alphastats.dilate_shift` computes the same kernel with
  big-int row shifts (rows padded by `d` columns so nothing wraps); the two
  agree exactly. Layers with no weak pixel, or no strong one, skip the
  dilation.
- `stray_specks` *(P5.10, 2026-11-25)*, next to `stray_count` (which stays
  the total): `{"count", "pixels", "regions", "samples", "truncated"}`.
  The stray pixels (`alphastats.stray_mask`: weak AND NOT grown, 0/1
  bytes) split into 8-connected components (`alphastats.components`);
  `count` is the number of *specks*, components of at most
  `stray_speck_max_px` pixels, `pixels` their total, `regions` the number
  of larger components (soft glows and auras: intended art, not
  flagged), `samples` the bounding boxes of up to 5 specks in canvas
  coordinates (top-most first, then left-most; `{x, y, w, h}`).
  `truncated` is true when the layer has more than 2,000,000 runs of stray
  pixels (a noise field): the scan stops there, and the counts cover the
  rows scanned. A layer with no stray pixel has `{"count": 0, "pixels": 0,
  "regions": 0, "samples": [], "truncated": false}`.
  `alphastats.stray_stats(alpha, w, h, below, distance, speck_max_px,
  dilate, origin)` returns `stray_count`, `stray_specks` and whether it
  dilated, in one call for the collector.
- Components: run-length union-find over rows. Each row's runs of 1s come
  from one `re.finditer(rb"\x01+")` (in C); a run `[a, b)` touches a run
  `[c, d)` of the row above iff `c <= b and a <= d` (8-connectivity); a
  two-pointer sweep pairs neighbouring rows' runs, so the Python work is
  O(runs + rows), never O(pixels). Union by size with path compression;
  each root keeps its size and bounding box. A 1000×1000 mask of 12 %
  noise plus a solid block takes about 0.05 s; 50 % noise (≈250k runs)
  about 0.1 s.
- Cost: the headless Mao rebuild (185 paint layers, 5800×8400) takes
  about 1.2 s, the slowest layer (the full-canvas guide image) about 0.3 s;
  the fallback is as fast.
- Lint reads the snapshot through `lint.run(snapshot, rules,
  severity_min)`; a missing `settings` key falls back to the default, an
  unknown `additive_mode` is `bad_request`.
- Hand-written snapshots for every rule are in `tests/unit/snapshots/`;
  `lint_four_findings.json` is acceptance test 4's document.

#### Lint rules

These restate the art repo's CONVENTIONS.md as checkable rules.
**Pixel rules apply only to paint layers.** The contents of reference
groups (name wrapped in brackets, `[ Guide ]`) are skipped by every rule
except `reference-hidden`.

*(P4.2, 2026-11-25)* A reference group is a **group** whose name,
trimmed, starts with `[` and ends with `]` (`[ Guide ]`, `[Refs]`); a
bracket-named paint layer is linted like any other. Everything inside one,
at any depth, is skipped. The bracket group itself is an ordinary group to
the default rules: its own name counts for `duplicate-name`, its layer
style is a finding, and so on.

`default` set: the spec's list, and what acceptance test 4 checks.

| Rule | Sev | Fires when |
|---|---|---|
| `node-type` | error | A node's type is not `paintlayer`/`grouplayer`: filter, fill, clone, vector or file layers, and all masks. Masks name their parent layer. The document's global selection (a root-level `selectionmask`) is exempt |
| `layer-style` | error | `layerStyleToAsl()` is not empty |
| `inherit-alpha` | error | `inherit_alpha` is true (Krita's clipping): bake it into the target with `merge_layers` |
| `blend-mode` | error | A paint layer's blend mode is not `normal`/`multiply`/`add`/`linear_dodge`. Groups and other node types are exempt, since group blending doesn't matter to the rig (other types are `node-type` findings). `add` and `linear_dodge` render identically and both count as the rig's Add (D17). With `lint.additive_mode` `"either"` (default) both pass; with `"add"` or `"linear_dodge"` the other spelling is a finding whose message names the required one and `krita_update_layers` |
| `duplicate-name` | error | Two or more nodes, anywhere in the document, share a name. **One finding per name.** `layer_id` is the second occurrence in stacking order, and `related_ids` lists all of them |
| `side-token` | warning | Within one parent, stripping side tokens makes names collide (`Eyebrow` next to `Eyebrow L`): flag the ones without a token (`related_ids`: the sided siblings). Also flags non-standard tokens such as `_L`, `Left`, `(L)` or a lowercase ` l`, with the standard spelling in the message |
| `empty-layer` | warning | A paint layer with no pixel where `alpha > 0` |
| `out-of-canvas` | warning | A paint layer's `opaque_bounds` reaches outside `(0, 0, width, height)` |
| `stray-alpha` | warning | A paint layer has stray **specks**: 8-connected groups of at most `lint.stray_speck_max_px` (default 64) stray pixels, a stray pixel being one with `0 < alpha < 8` further than 4 px from any pixel with `alpha ≥ 8`. Larger groups of stray pixels (soft glows and auras fading out over more than 4 px) don't count, nor do anti-aliased edges. All three thresholds are configurable. A layer too fragmented to analyse (`stray_specks.truncated`) is a finding of its own. *(P5.10, 2026-11-25: was any stray pixel, which flagged 48 of Mao's layers, mostly intended glows; decided with the user.)* |
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

*(P5.9, 2026-11-25)* Settled from CONVENTIONS § Structure and § Naming
(`lint.py`; reference group contents are skipped as by every rule):

- `nesting-depth`: a **paint layer** with more than 3 group ancestors
  (`depth` > 4; CONVENTIONS' `Head/Eyes/Eyeballs` is 3 groups). One
  finding per paint layer; groups and other node types are exempt.
- `name-case` (`lint.name_case_problems`), paint layers and groups only:
  words separated by single spaces, no leading/trailing space, no `_`.
  Each word, and each part of a hyphenated word, must start (after an
  opening bracket or quote) with an uppercase letter or a digit. Allowed:
  side tokens `L`/`R`, variant letters `A`/`B`, numbers and number-led
  words (`2`, `3D`), acronyms and inner capitals (`FX`, `McKay`), words
  without letters or digits (`&`, `[`, `]`, so `[ Guide ]` passes), and
  lowercase minor words (`a an and as at but by for from in into nor of
  on onto or per the to via vs with`) except as the first or last word
  (so `Back of Head` passes and `Arm L a` doesn't). Uncased letters
  (Japanese) are reported as not English. The message lists the problems
  and, when raising first letters and replacing `_`/extra whitespace fixes
  them, the suggested name.
- `blend-suffix`, paint layers only: the suffix is the last word of a
  name of two or more words, recognised in any case. `multiply` wants
  ` Multiply`; `add` and `linear_dodge` want ` Add` (D17). Findings: the
  suffix missing; a suffix for the other mode; the suffix on a layer in
  another mode (normal, screen…; the fix names `multiply`, or the Add
  spelling `lint.additive_mode` requires, `add` for `"either"`); the right
  suffix in the wrong case (`multiply`).
- `pose-visibility`: sibling **groups** whose names end in a space and one
  capital other than `L`/`R` (`lint.pose_parts`), grouped by the rest of
  the name. Only families of two or more that include `A` are checked:
  `A` hidden, or any other member visible (own `visible` flags; siblings
  share their ancestors). `related_ids`: the rest of the family. Paint
  layers named like poses, families without `A` and lone `<X> A` groups
  are not checked.
- `reference-hidden`: a reference group whose own `visible` is true, even
  inside a hidden group. Nested reference groups are contents, so skipped.
- `mask-suffix` (`lint.clipping_bases`): the base of an `inherit_alpha`
  node is the nearest sibling below it (same parent; masks are not
  siblings) that doesn't inherit alpha itself, so a stack of clipped
  layers shares one base, as in the clipping-mask idiom the art uses
  (Krita clips to the whole stack below within the group, which is that
  base). The base, a layer or group, fires when its last word isn't
  ` Mask`; `related_ids` are the clipped layers. An `inherit_alpha` node
  with nothing below it has no base.

Side tokens, settled in P4.2 from CONVENTIONS § Naming (`<Thing> <Side>
<Variant/Index>`, ` L` / ` R` as separate words, the character's side):

- Standard: the word `L` or `R`, not the first word: `Eyebrow L`,
  `Arm L A`, `Highlight R 2`.
- Non-standard, still read as a side: a lowercase word `l` / `r`;
  `Left` / `Right` as a word in any case; `(L)` / `[R]` in any case; a
  side letter or word joined by `_`, `.` or `-` (`Eye_L`, `L_Eye`,
  `Hand.r.B`, `Hair L-2`); `L` / `R` as the first word (`L Eye`). A name
  that is a single plain word (`L`, `Left`) has no side.
- The suggested name strips the token and puts ` L` / ` R` before the
  trailing variant/index words (single capitals, numbers):
  `Left Hand B` → `Hand L B`, `Eye_l_2` → `Eye L 2`.
- Collisions compare the stripped names of siblings, so a non-standard
  token counts as a side there too (`Body` next to `Body_L` flags both:
  `Body` for having no token, `Body_L` for its spelling).

Other settled details: `duplicate-name` is case-sensitive and counts every
node, masks and groups included. `color-space` needs `"sRGB"` in the
profile name, case-sensitively, so `Rec2020-elle-V4-srgbtrc.icc` (sRGB
curve, wide gamut) is a finding. `out-of-canvas` gives the overhang per
side (`3 px left, 6 px below`); `stray-alpha` gives the number of specks,
their pixels, the thresholds from `settings` and up to 3 sample rectangles
(`(x, y) wxh`, canvas coordinates). On a snapshot without `stray_specks`
(a collector from before P5.10) it falls back to `stray_count > 0` with
the old message (the count and both thresholds).

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
| `krita_health` | Adds `protocol`, `version`, `krita_version` (done in P1.3, with `running` and `protocol_ok` added server-side), then `allowed_dirs` (resolved roots, the output dir first), `config_file`, `config_loaded`, `limits` and `config_errors` (done in P1.4; the summary counts the dirs and lists the errors), then `busy` (`{"action", "elapsed_ms"}` or `null`) and `queued` (done in P1.5; the summary adds e.g. "busy: save for 12.3 s, 1 queued"), then `lint` (the config's lint settings, P4.4) |
| `krita_new_canvas`, `krita_open_file`, `krita_save` | As above. Done in P1.6: `new_canvas` and `open_file` return `doc` (the new document's handle; the registry keeps the wrapper, so a headless `new_canvas` leaves an open document) and `save` takes `doc`. The rest done in P1.7 |
| `krita_get_canvas` | Takes `doc` (done in P1.6). A full-canvas `render` into the output dir (done in P3.4; see § `krita_render`): the projection (8-bit sRGB for any document) instead of `exportImage`; the result adds `render`'s fields |
| `krita_set_color` | Stored plugin-side (D15) and also set on the active view when there is one; works without a view. Returns `color` (normalized `#rrggbb`) and `view_updated`. An invalid color is `bad_request`. Painting uses the stored color, else the view's foreground, else black. **No `doc`**: the color is plugin-global state that applies to every document, which is what lets painting work in a document with no view (D15). Done in P1.6 |
| `krita_set_brush` | `size` and `opacity` are stored plugin-side (the stroke defaults) and `size` is also set on the view if there is one; a `preset` needs a view (`conflict` without one, and nothing is stored). Returns the stored `preset` (matched name or null), `size`, `opacity` and `view_updated`. No `doc`, like `set_color`. Done in P1.6 |
| `krita_list_brushes` | Structured results only |
| `krita_stroke`, `krita_fill`, `krita_draw_shape`, `krita_clear` | Accept `doc`, `layer`. `layer` defaults to the document's active node; anything but a paint layer (including an active group or mask) is `conflict` with `details: {doc, layer, type}`. Headless there is never an active node, so `layer` is required there (`not_found` otherwise). No view needed. Results include `doc` and `layer` (id). `clear` takes `"transparent"`, which zeroes the layer over the union of its bounds and the canvas; a color fills the canvas rect. Done in P1.6. Since P5.1 each call is one agent journal entry (a `RestorePixels` of the rectangle it writes), so `krita_undo` reverts it; the result gains `warnings: [{code: "not_undoable", nbytes}]` only when the snapshot is over the journal's caps |
| `krita_get_color_at` | Accepts `doc`, `layer` (sample one layer instead of the composite): a paint layer gives its own pixel, a group its projection, other types `conflict`. `0 <= x < width`, `0 <= y < height` of the document, else `bad_request` naming the size. Results include `doc` and `layer` (id, or null for the composite). Done in P1.6 |
| `krita_undo(doc?)` / `krita_redo(doc?)` | The agent journal (D6), done in P5.1: one call applies one journal entry, i.e. reverts (re-applies) one agent tool call: `create_layers`, `update_layers`, `move_layers`, `delete_layers`, `import_png`, `stroke`, `fill`, `draw_shape`, `clear`. Returns `{doc, undone \| redone: {label, layers: [ids touched]}, remaining: {undo, redo}, warnings}`. Empty stack: `conflict` ("nothing to undo in doc-N"). An entry whose layers are gone (deleted by hand, say) is `conflict` with nothing changed, and stays on the stack; a Krita failure midway is rolled forward again and keeps its code (`krita_error`), see D6. A new agent edit clears the redo stack. `warnings` has `not_redoable` / `not_undoable` when the reverse entry is over the caps. Mutating, default timeout 300 s |
| `krita_journal(doc?)` | New in P5.1: the journal's stacks, read-only: `{doc, undo: [{label, layer_count, nbytes, disk_bytes, on_disk}], redo: [same], memory_bytes, disk_bytes, limits: {max_actions, max_bytes, max_disk_bytes}}`, the next entry to apply first |
| `krita_ui_undo(doc?)` / `krita_ui_redo(doc?)` | New in P5.1: the old `edit_undo` / `edit_redo` action triggers on Krita's own undo stack, aimed at the doc's view: a `doc` that isn't the active document gets its view shown first; a document with no view is `conflict` (always so headless). Returns `{doc, action, activated_view, warnings: [{code: "partial_ui_undo", message}]}`, the D6 caveat that Krita's undo only partly covers agent edits |
