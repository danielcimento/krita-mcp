# F003 decisions

Numbered E1… so they don't collide with F001's D1–D17. Status is
**Accepted** unless marked otherwise.

## E1. The 3.3 difference was Krita's tile grid, not our PNG path

**Context.** The work order (K1) read the smoke test's 316 ±1 pixels and
2,870 alpha-0 pixels as a premultiplied-alpha round trip, and asked for
straight-alpha PNG I/O.

**Finding.** Reproduced headless on `mao_pro_layered.kra` (plan.md has
the table):

- Every one of the 184 paint layers came back byte-identical through
  `export_layers` → `create_layers`, and every property matched. The PNG
  path (`pixels.save_png` / `load_png_bgra`, `QImage::Format_ARGB32`) was
  already straight alpha and byte-exact, as F001's spike Q5 found.
- The difference was only in Krita's composite. In the source `.kra` every
  paint layer's paint device has a non-zero origin (`Node.position()`, for
  example (1603, 4423)), which is where its 64 px tile grid starts. A layer
  made by `createNode` starts at (0, 0). Krita's compositing rounds by ±1
  in soft, low-alpha areas depending on where tile boundaries fall, and it
  leaves different colour under alpha 0.
- Giving each rebuilt layer the source's origin (`Node.move(x, y)` on the
  empty layer, then `setPixelData` at canvas coordinates) made the
  composite match: 0 visible differences.

**Decision.** `export_layers` records each paint layer's `device_origin`
in the manifest, and `create_layers` applies it (specs and manifests,
optional, default (0, 0)). The PNG path stays as it was. The work order
also asked for a single conversion helper: there already is one
(`kritamcp/pixels.py`), and every PNG read or write goes through it. Its
docstring now says so. `render`'s scaling (scale ≠ 1) uses a 16-bit
premultiplied working image; that is resampling, not a round trip, and is
documented in `commands/render.py`.

**Consequences.** A rebuild from an F003 manifest is byte-identical to its
source, composite included. Manifests without `device_origin` (F001's, or
`tools/ora_to_manifest.py`'s) still work and can show the ±1 drift; K4's
diff reports it. Rebuilds of rebuilds were already stable (origin (0, 0)
both times).

## E2. The export crop keeps colour under alpha 0

**Context.** After E1, 11 alpha-0 pixels still differed: colour under
alpha 0 just outside each layer's alpha bounds. `Node.bounds()` only covers
pixels with alpha > 0, and the export cropped to that.

**Decision.** For each paint layer, `export_layers` reads the layer's
bounds grown to whole tiles of its paint device (`tile_window`: the grid
starts at `device_origin`). It crops to the layer's alpha > 0 pixels plus
any alpha-0 pixels in that window that carry colour. Outside `bounds()`,
pixels with alpha > 0 are the layer's default pixel, not content (the
white `Background` that `createDocument` makes reads as opaque
everywhere), so they don't widen the crop.

**Consequences.** The Mao round trip is byte-identical, alpha-0 colour
included. A PNG can now be a few pixels larger than the layer's alpha
bounds; the manifest's `x, y, w, h` describe the PNG, and `list_layers`'
`bounds` remain the alpha bounds. Colour under alpha 0 in a tile with no
visible pixel is still dropped: Krita's Python API doesn't expose a
layer's tile extent, and such colour has no visible effect.
`diff_documents` reports alpha-0 differences on their own line (E5).

## E3. Config changes need no restart; `krita_reload_config` anyway

**Context.** K2 asked for "restart Krita" in the fix line, or a
`krita_reload_config` tool if the plugin could reload without a restart.

**Decision.** The plugin has re-read the config file on change since F001
(`config.current()`), so the `forbidden_path` message says "it applies
from the next call, no restart needed". `krita_reload_config` is added
anyway: it forces a re-read and returns the result, so an agent can check
its edit without making a file call that might fail. The message also
gives the folder to add (the nearest existing folder that contains the
path, never home or above) and the JSON to write, with the existing
entries kept. A unit test applies that JSON and checks the path is then
allowed.

## E4. `duplicate-name`: two namespaces, and fixes in data as well as text

**Decision (CONVENTIONS § Naming, updated with the work order).** Groups
are checked against groups, and every other node (paint layers and the
other layer types) against each other. When each copy sits under a
different pose group (`<X> A`, `<X> B`), the finding carries
`suggested_names: [{id, name}]` with the pose suffix, so one
`update_layers` call applies the fix. Otherwise the message suggests an
index or a name that says what differs, with no data: which copy is
which is the artist's call.

**Result on Mao.** 11 findings, all A/B pairs, fixed in one call
(`tests/krita/test_smoke_sizes.py`).

## E5. Pixel diffs: counts, bounds, max difference, alpha-0 apart

**Decision.** `diffplan.diff_stats` replaces `diff_region`. For the
composite and for each matched layer it returns `changed_count` (visible:
alpha > 0 in either), `bounds`, `max_channel_diff`, `alpha0_rgb_count` and
`alpha0_rgb_bounds`. `max_channel_diff` comes from 16-bit lanes in big
ints (`max_abs_diff`), so there is still no per-pixel Python loop.
`ignore_alpha0_rgb` (default true) keeps alpha-0-only differences out of
`projection_changed` and `pixel_changes`. They are still counted
(`projection.alpha0_rgb_count`, `counts.alpha0_rgb_only_layers`). Layer
comparisons use the tile window (E2), so hidden colour next to a layer is
seen.

## E6. `new_document` lives on `create_layers`

**Decision.** `create_layers(manifest=…, new_document=true)` creates the
document from the manifest's `document` block (size, resolution,
profile; RGBA/U8 only) with no layer, builds into it, and closes it again
on failure; `name` overrides the block's. A dry run creates and closes a
document to validate against. The document is left modified, since it
holds unsaved work. `new_canvas(background="none")` gives the same empty
document for spec-based builds.

## E7. `list_layers` pages by default

**Context.** K3's default fields (id, name, type, visible, blend_mode,
children) still cost about 190 bytes a layer: 42 KB for Mao's 222 nodes,
against an 8 KB target.

**Decision.** The tool lists 30 layers by default (`limit`, `offset`,
`next_offset`), with an `overview` of the whole listing: counts by type,
max depth, hidden layers, and non-normal blend modes (exact counts, up to
20 paths each). `summary_only` returns just the overview. `path` writes
every field of every layer to a JSON file. The key is `overview` because
`summary` is already the tool's one-line text. `fields=["all"]` gives
every field.

## E8. Compact defaults live in the MCP tools, not the plugin commands

**Decision.** The plugin commands keep their F001 defaults (full
listings, every created layer, flat lint findings, every export entry).
The new parameters (`limit`, `overview`, `verbose`, `group_by`, …) are
optional, and `server.py` sends the compact values by default:
`list_layers` 30 layers with overview, `export_layers` `verbose=false`,
`create_layers` `verbose="auto"` (at most 20 created layers listed),
`lint` `group_by="rule"` with 5 findings per rule.
`tests/unit/test_server_tools.py` pins what the tools send, and
`tests/krita/test_smoke_sizes.py` replays the smoke test with those
parameters. Internal callers (the headless suite, scripts) keep the full
results.

**Protocol 3.** A protocol-2 plugin would ignore or reject the new
parameters and quietly return the old shapes, so the protocol is bumped
and `server.py` refuses to talk to an older plugin, with "update the
plugin and restart Krita".

## E9. A unit test was reaching the user's Krita

**Found while running the suite.** `test_export_layers_schema_and_typos`
expected `crop="no"` to fail validation, but pydantic accepts "no" as
false. The call went through to the running Krita and exported its active
document into `~/krita-mcp-output/out`. It only passed before because
Krita usually wasn't running.

**Decision.** `_call_tool` in `tests/unit/test_server_tools.py` now
replaces `client.call` with a guard that records the call, and a call
that reaches the guard counts as not an error. The test uses
`crop="maybe"`. Unit tests can no longer touch a live Krita.
