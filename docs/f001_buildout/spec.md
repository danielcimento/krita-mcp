# Spec: krita-mcp extensions for Live2D art preparation

> **Source document.** This is the requested spec as received, kept for
> reference. Links to `CONVENTIONS.md` pointed into the art repository that
> consumes this tool, so they are plain text here. The rules the spec relies
> on are restated in [api-design.md § Lint rules](api-design.md#lint-rules).
> The plan for building this is in [plan.md](plan.md).

Status: draft · Target repo: `~/krita-mcp` (`server.py` + `krita-plugin/kritamcp/__init__.py`)

## Goal

Let an agent prepare and maintain rig-ready layered artwork in Krita:
inspect and restructure the layer tree, move pixels in and out of
individual layers, render what it changed, and check the document against
the art repo's CONVENTIONS.md before handing it to umamo.

The current tool set (canvas, brush, stroke, shapes, open/save) is built
for freehand painting. That is the least useful capability for this
workflow: agents are poor freehand painters, but good at structured edits
and at pixel work done in Python and written back.

## Observations from the first session

| Observation | Where | Consequence |
|---|---|---|
| No layer API at all | plugin dispatch | Building a 184-layer document needed a hand-written OpenRaster file opened via `open_file` as a workaround |
| `MAX_BODY_BYTES = 5 MB` | plugin config | Pixel data cannot be sent inline in JSON for real layers; transfer must go through files |
| `MAX_CANVAS_PIXELS = 4096*4096` | plugin config | `new_canvas` refuses typical Live2D canvases (the Mao demo model is 5800x8400 = 48.7 MP). `open_file` is unaffected, so the limit is inconsistent too |
| Files only under `~/krita-mcp-output` | `ALLOWED_DIRS` | Every result had to be copied into the repo by hand |
| `krita_save` echoes the requested path, not the resolved one | `server.py` | The plugin returns `path`, but the wrapper drops it, so the caller had to guess where the file went |
| `open_file` reported an empty document name for `.ora` | plugin | Cosmetic, but documents need stable handles anyway (below) |
| Everything acts on "the active document" | `get_active_document()` | After opening several files, which one a call hits depends on UI state |
| Tools return prose strings | `server.py` | Callers have to parse English, which is brittle for chaining |

## Design principles

1. **Stable handles.** Every tool takes an optional `doc` (handle returned by
   `open_file` / `new_canvas` / `list_documents`; default: active
   document) and addresses layers by Krita's node UUID
   (`Node.uniqueId()`), never by name or index. Also accept a name path
   (`"Head/Eyes/Eyeball L"`) as a convenience, and reject it when it
   matches more than one layer.
2. **Structured results.** Return JSON objects (FastMCP supports dict
   returns), and keep a short human-readable `summary` field.
3. **Files for pixels.** Pixel transfer goes through PNG files inside the
   allowed directories. Never send pixels inline. This sidesteps the body
   limit and lets agents process images with any tool.
4. **Batch, then one undo step.** Structural edits accept lists and run
   inside one undo macro, so one `undo` reverts one agent action.
   (Check whether Krita's Python API exposes undo grouping. If not, document
   the granularity.)
5. **Configurable roots.** `ALLOWED_DIRS` comes from config
   (`KRITA_MCP_ALLOWED_DIRS`, path-separated, or a config file next to the
   token), so a project repo can be allowlisted. Keep the extension
   allowlist and the existing path-traversal checks.
6. **Long-running calls.** Saves, exports and renders of large documents
   can take tens of seconds. Use generous timeouts and return
   `elapsed_ms`.

## Tools

Priority: **P0** blocks the workflow, **P1** strongly wanted, **P2** later.

### Documents

| Tool | Pri | Purpose |
|---|---|---|
| `list_documents()` | P0 | Open docs: handle, name, file path, size, modified flag, active |
| `get_document_info(doc)` | P0 | Size, resolution, color model/depth/profile, layer and group counts |
| `new_canvas(...)` | P0 | Existing tool; raise the pixel cap (for example to 8192x8192), add `color_profile`, transparent background option, return handle |
| `open_file(path)` | P0 | Existing; return handle and resolved path |
| `save(doc, path?, format?)` | P0 | Existing; return the resolved path. `.kra` uses `saveAs`, so the doc's file path follows. Other formats use `exportImage` (keep batch mode) |
| `close_document(doc, discard?)` | P1 | Refuse to close a modified doc unless `discard: true` |
| `set_canvas_size(doc, w, h, anchor)` | P2 | Resize the canvas without scaling content |

### Layer tree

| Tool | Pri | Purpose |
|---|---|---|
| `list_layers(doc, root?, depth?)` | P0 | Tree of `{id, name, type, parent, index, visible, opacity, blend_mode, locked, alpha_locked, inherit_alpha, pass_through, bounds:{x,y,w,h}, children}`. `type` is Krita's node type string (`paintlayer`, `grouplayer`, `filterlayer`, `clonelayer`, `vectorlayer`, `filllayer`, `filelayer`, `*mask`) |
| `create_layers(doc, specs[])` | P0 | Batch create paint/group layers: `{name, type, parent, above?, visible, opacity, blend_mode, png?, x?, y?}`. With `png`, the file's pixels go in at offset `(x, y)`. This one call replaces the whole OpenRaster workaround |
| `update_layers(doc, changes[])` | P0 | Batch set name/visible/opacity/blend_mode/locked/alpha_locked/inherit_alpha |
| `move_layers(doc, moves[])` | P0 | Reparent and/or reorder: `{id, parent, above? / below? / index?}` |
| `delete_layers(doc, ids[])` | P0 | |
| `duplicate_layer(doc, id, name?, parent?)` | P1 | |
| `merge_layers(doc, ids[], name)` | P1 | Merge down or merge a group to one paint layer (for baking clipping, filters, styles) |
| `set_active_layer(doc, id)` | P1 | So the existing brush tools paint where intended |

### Pixels

| Tool | Pri | Purpose |
|---|---|---|
| `export_layers(doc, ids[] or "all", dir, crop=true, include_hidden=true)` | P0 | One PNG per layer, cropped to bounds, plus `manifest.json` with id, name path, offset, size, blend mode, visibility and stacking index. Use `Node.save()` with an export rect, or `pixelData()` written to PNG |
| `import_png(doc, id, png, x, y, mode="replace"\|"over")` | P0 | Write a PNG into an existing paint layer at an offset |
| `render(doc, path, scale?, region?, only?: ids[], hide?: ids[])` | P0 | Composite to PNG, optionally limited to a layer subset and region, without changing the document's visibility state permanently. The agent's "eyes" |
| `trim_layers(doc, ids[], alpha_threshold=1)` | P1 | Clear stray near-transparent pixels below the threshold |
| `layer_stats(doc, ids[])` | P1 | Opaque bounds, opaque pixel count, alpha histogram buckets, and whether the layer is empty |

Note: `pixelData()` / `setPixelData()` use BGRA byte order for 8-bit RGBA
documents. The current `clear` handler already does this, so keep the
conversion in one helper.

### Selections and part separation

| Tool | Pri | Purpose |
|---|---|---|
| `select(doc, mode, ...)` | P1 | `rect`, `polygon(points)`, `alpha_of(layer_id, threshold)`, `invert`, `none`; combine with `replace / add / subtract / intersect` |
| `selection_to_layer(doc, source_id, name, parent, cut=true)` | P1 | Copy or cut the selected pixels of a layer into a new layer at the same position. The core operation for splitting concept art into parts |
| `transform_layer(doc, id, translate?, scale?, rotate?, flip?: "h"\|"v", pivot?)` | P1 | Includes flipping for mirrored parts |

### Live2D-specific analysis

| Tool | Pri | Purpose |
|---|---|---|
| `lint(doc, rules?)` | P0 | Check against CONVENTIONS.md: disallowed node types (filter/fill/clone/vector/file layers, masks), layer styles, `inherit_alpha`, blend modes outside Normal/Multiply/Add, duplicate names (document-wide), names missing side tokens where a mirrored sibling exists, empty layers, layers extending beyond the canvas, stray alpha, non-8-bit or non-sRGB documents. Return `[{rule, severity, layer_id, message}]` |
| `occlusion_map(doc, id, path)` | P1 | Which pixels of a layer are covered by visible layers above it (PNG mask plus covered %). Shows what is hidden at rest, and so which areas need painting underneath before they can move |
| `diff_documents(doc_a, doc_b)` | P2 | Layer-level diff, matched by UUID then name: added, removed, renamed, moved, property changes, and pixel changes (changed-pixel count and bounds) |

### Existing tools

Keep the painting tools. Make `krita_save` return the plugin's resolved
`path`, and make all tools accept `doc`. Add `krita_` prefixes
consistently to new tools, or drop them from all; either works, but pick one.

## Acceptance tests

1. **Mao rebuild without the workaround.** Given the 184 PNGs and the
   manifest from the first session, build the document with one
   `create_layers` call (nested groups, offsets, Multiply on 7 layers,
   two hidden groups), save as `.kra`, then `list_layers` and check 184
   paint layers, 38 groups and matching names.
2. **Round trip.** `export_layers(all)` then `create_layers` from the
   manifest into a new document gives a pixel-identical `render`.
3. **Canvas cap.** `new_canvas(5800, 8400)` succeeds.
4. **Lint.** A fixture `.kra` with a filter layer, a clipped layer, a
   duplicate name and an empty layer gets exactly those four findings.
5. **Allowlist.** With the repo allowlisted, `save` writes into it.
   Paths outside every root are still refused, including `..` and symlinks.
6. **Handles.** With two documents open, calls with `doc` hit the
   right one regardless of which is active in the UI.

## Out of scope

- Generative painting or inpainting inside Krita. Do it outside, then write it back with `import_png`.
- Animation timeline features.
