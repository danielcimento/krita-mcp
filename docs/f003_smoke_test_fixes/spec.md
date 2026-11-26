<!-- The work order as received, verbatim: live2d-workspace @ 6bf962d,
docs/specs/krita-mcp-extensions-v2.md. Relative links point into that repo.
What was built, and where it departs from this, is in plan.md and decisions.md. -->

# Spec v2: krita-mcp fixes after the first smoke test

Status: draft · Target: `~/krita-mcp` (plugin `kritamcp` 0.2.0, protocol 2, @ a6549d4)
Builds on: [krita-mcp-extensions.md](krita-mcp-extensions.md) (v1)
Evidence: smoke test run 2026-11-25. The report is on branch
`smoke-test/2026-11-25` at `smoke-out/REPORT.md`, uncommitted.

## Where v1 landed

The v1 surface is in place and was easy to find: every krita-mcp step in
the smoke test was found on the first try (rating A). `create_layers`
rebuilt the 222-node document in one call, `update_layers` batched 28
renames as one undo step, a selection split was reverted with one
`undo`, and the occlusion map and mirror steps produced correct results.
**Keep all of that.** This spec covers the one functional failure and
the legibility issues.

Configuration note: the allowlist miss in the run was a configuration
problem, now fixed with `~/.krita-mcp-config.json` →
`{"allowed_dirs": [...]}`. K2 makes that discoverable from the tools.

## Changes

Priority: **P0** fails the smoke test, **P1** a legibility finding that
costs agents real effort, **P2** polish.

### K1 · Lossless layer pixel round trip — P0

**Problem.** `export_layers` → `create_layers` isn't pixel-identical
(smoke test 3.3):
- 316 visible pixels in soft glow areas (Healing Magic, Aura, wand) differ by exactly ±1 in RGB.
- 2,870 fully transparent pixels differ only in their hidden RGB.

That's the premultiplied/straight alpha rounding signature, most likely
from going through `QImage::Format_ARGB32_Premultiplied` (or a
premultiplied blit) on export or import.

**Required.**
- **Byte-exact pixels:** moving 8-bit RGBA layer pixels between Krita and PNG is byte-exact both ways for every pixel with alpha > 0. Use `Node.pixelData()` / `setPixelData()` on straight BGRA bytes, and encode and decode PNGs as straight RGBA (`QImage.Format_RGBA8888` or `Format_ARGB32`, never `*_Premultiplied`), or write the PNG without Qt.
- **Transparent pixels:** keep the RGB of alpha-0 pixels where the path allows it. If a step can't (some encoders zero them), document it, and make `diff_documents` report it separately (K4).
- **One helper:** keep the conversion in one helper used by `export_layers`, `create_layers(png/manifest)`, `import_png` and `render`.

**Acceptance.**
1. Round trip `mao_pro_layered.kra` through `export_layers(all)` → `create_layers(manifest)`. Compare every layer's `pixelData()` byte for byte: 0 differing pixels with alpha > 0.
2. The composite renders are identical for alpha > 0.
3. A synthetic layer with a 0–255 alpha ramp across saturated colours survives 10 round trips unchanged.

### K2 · Allowlist errors that name the fix — P1

**Problem.** `forbidden_path: Path is outside the allowed directories: <path>`
doesn't say which folders are allowed or how to add one.
`krita_health` names the config file but not the key.

**Required.** Every `forbidden_path` error includes the allowed folders
(expanded), the config file path, the key name, and a one-line fix:
```
forbidden_path: /repo/smoke-out/x.png is outside the allowed directories
  allowed: /Users/me/krita-mcp-output, /Users/me/smoke-out
  to allow it: add the folder to "allowed_dirs" in /Users/me/.krita-mcp-config.json, then <restart Krita | call krita_reload_config>
```
`krita_health` returns `config_path`, `config_key: "allowed_dirs"`,
the resolved `allowed_dirs` and whether the config file exists. If the
plugin can reload config without restarting Krita, add a
`krita_reload_config` tool. Otherwise say "restart Krita" in the message.

**Acceptance.** An agent given only a `forbidden_path` error can write
the correct config change without reading code or docs.

### K3 · Bulk results that fit in context — P1

**Problem.** `list_layers` returned 66 KB and `export_layers` 72 KB for
the 184-layer document. Both overflowed the agent's context and spilled
to files.

**Required.**
- **`list_layers`:**
  - `fields` to choose which properties to return (default: `id, name, type, visible, blend_mode, children`).
  - `summary_only: true` returns counts by type, hidden nodes, non-Normal blend modes and maximum depth.
  - `path` writes the full tree to a JSON file and returns just the path and summary.
- **`export_layers`:** return the manifest path, counts, total bytes and any skipped layers. Stop echoing every layer by default (`verbose: true` brings it back).
- **`lint`:** `group_by: "rule"` (default) returns per-rule counts plus the first N items, with `limit` and `offset`.
- **Size guideline:** no tool's default result should exceed about 8 KB on a document of around 200 layers.

**Acceptance.** Every krita-mcp call in the smoke test returns at most
8 KB by default on `mao_pro_layered.kra`.

### K4 · `diff_documents` quantifies composite changes — P1

**Problem.** After the rebuild, `diff_documents` said
`projection_changed: true` with every per-layer list empty, giving no
idea of scale or location.

**Required.** When the composite differs, report:
- the number of changed pixels
- the bounding box
- the maximum per-channel difference
- the count where only alpha-0 RGB differs, which is usually harmless

Do the same for each layer whose pixels changed. Add
`ignore_alpha0_rgb: true` (default true) so harmless hidden-colour
differences don't set `projection_changed`.

**Acceptance.** For the 3.3 situation (before K1 is fixed), the diff
reports 316 visible pixels with maximum difference 1, plus 2,870 alpha-0
pixels, with bounds, and no longer reports a bare `true`.

### K5 · Faithful document properties on rebuild — P1

**Problem.** `new_canvas` defaults to 120 dpi where the source was 72,
so a faithful rebuild was flagged as a resolution change. Rebuilding
also meant deleting the default Background layer by hand.

**Required.**
- **Document block:** `export_layers` writes a `document` block to the manifest (size, resolution, colour model and profile).
- **New document from a manifest:** `create_layers(manifest=…, new_document=true)` creates the document from that block, with no default layer, and returns its handle.
- **Blank canvases:** `new_canvas` gets `background: "none"` for a document with no initial layer.
- **Rebuild cost:** that brings a full rebuild down to one call.

**Acceptance.** Smoke test 3.2 is one call, and the diff of the rebuild
against the original reports no document-level differences.

### K6 · Lint name rules match the conventions — P1

**Problem.** `lint` reported 17 duplicate names against the expected 11.
The extra 6 were groups sharing a name with a layer inside them (Front
Hair, Nose, Face, Neck, Hood, Hat Underside). The conventions didn't
cover that case, so the agent invented ` … Parts` group names.

**Decision (CONVENTIONS.md updated alongside this spec).**
- **Separate namespaces:** groups and paint layers each need unique names among their own kind.
- **Group and layer may match:** a group may share its name with a paint layer. That's normal: the Face part contains the Face layer, as in Live2D's own sample.
- **Why it's safe:** reimport now matches by layer UUID, so names no longer carry identity.

**Required.**
- **Scope:** `duplicate-name` checks paint layers against paint layers and groups against groups, never one against the other.
- **Message:** each finding names the conflicting layers with their group paths and suggests the conventions' fix, for example "rename to `Hand L A` / `Hand L B` (pose suffix)".

**Acceptance.** `lint` on `mao_pro_layered.kra` reports exactly 11
`duplicate-name` errors, all A/B pose pairs.

### K7 · Polish — P2

- **`stray-alpha`:** the 33 warnings should include each layer's affected pixel count and bounds, so an agent can decide whether to trim (`trim_layers`) without rendering.
- **Colour before drawing:** the repaint step needed `set_color` before `draw_shape`. Let `draw_shape` and `stroke` take an optional `color`.

## Regression checks (keep passing)

- One `create_layers` call builds nested groups, offsets, blend modes and visibility.
- `update_layers` and `create_layers` batches are one undo step.
- `selection_to_layer(cut)` + one `undo` restores the original exactly.
- `occlusion_map` covered percentage and mask, `duplicate_layer` + `transform_layer(flip)` mirror placement, `layer_stats` counts.
- `new_canvas(5800, 8400)` succeeds.
- Paths outside the allowlist are still refused, including `..` and symlinks.
