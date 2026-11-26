# F005 tool contract changes

Deltas against F004's [api-changes.md](../f004_live2d_opt_in/api-changes.md).
The tool docstrings in `server.py` are the full current contract.
Protocol **3** (unchanged), plugin **0.5.0**, server **0.5.0** (H8).

## Config file (`~/.krita-mcp-config.json`)

| Key | Default | Meaning |
|---|---|---|
| `paint.check_visibility` | `true` | New (H3). After each painting call, render what covers the changed pixels and report `covered_pct`, `top_cover` and `notes`. `false`: those are left out (`covered_pct` null); `changed` and `layer_visible` stay |

`krita_health`, `/health` and `krita_reload_config` report the resolved
section as `paint` (`null` if the config couldn't load).

## `krita_health`

New, always present, also when Krita is down (K15, H8):

- `server_version`: this MCP server's version, `"0.5.0"`, equal to the
  plugin's.
- `server_commit`: the git commit the server runs from (full SHA), or
  null outside a git checkout.
- `server_dirty`: uncommitted changes to tracked files in that checkout,
  or null.

The summary adds "; server 0.5.0 (commit a4fe3ce2, dirty)". `version`
and `plugin` are still the plugin's.

## `krita_stroke`, `krita_fill`, `krita_draw_shape`, `krita_clear`

New result fields (K11, H2–H4):

| Field | Meaning |
|---|---|
| `changed` | `{x, y, w, h}` of the pixels that now differ from before the call, or null when none did |
| `changed_px` | How many pixels changed (null for a layer that isn't 8-bit RGBA, whose `changed` is then the written rectangle) |
| `layer_visible` | The layer and every group above it are visible |
| `covered_pct` | The share (0–1) of the changed pixels on the canvas that the layers drawn after it cover, as `krita_occlusion_map` measures it with its defaults. Null when the layer is hidden, nothing changed, or the check is off |
| `top_cover` | `{id, path}` of the layer above whose own pixels cover the most of the change; absent when none does |
| `notes` | `[{code, message}]`, present only when non-empty: `nothing_changed`, `layer_hidden` ("…: the layer is hidden" / "…: the layer's group 'X' is hidden"), `change_covered` (every changed pixel covered: "this change isn't visible in the composite: 100% covered by layers above (top cover: 'Neck')", or "hardly shows" under a partly transparent cover) |

The summaries add "; changed 60x40 at (10, 20), 100% covered by layers
above; note: …".

## `krita_list_layers` (command `list_layers`)

- **Fields** (K12, H5). `child_count` is a new field, always included
  (with `id` and `name`). `children` is an ordinary field now: listed
  only when asked for (`fields: ["children"]`, `["all"]`, `path=`).
  `nodes.ALWAYS_FIELDS` is `id, name, child_count`.
- **Default fields** (server): `id, name, type, visible, blend_mode`, no
  `children`.
- **Page guard** (server). A page whose JSON would pass 7.5 KB is cut to
  the rows that fit. `next_offset` moves to the first row left out, and
  `trimmed: {rows, of, max_bytes, dropped_fields}` appears. The summary
  says "page cut from 100 to 35 rows to stay under 8 KB". `children` is
  dropped (and named in `dropped_fields`) only when a single row doesn't
  fit.

`krita_diff_documents` ignores `child_count`, as it ignores `children`.

## `not_found` for a layer ref

For a layer name or path that matches nothing (K13, H6), the message
ends "; did you mean 'Hat/Hat Top', 'Hat/Hat Brim'?" (up to 5, nearest
first), and `details.suggestions` is `[{id, path}]`. Unchanged for a
UUID or a `$key`, and when nothing is similar.

## `krita_lint` (command `lint`)

- **`fix`** (K14, H7). Findings of `stray-alpha`, `empty-layer`,
  `out-of-canvas`, `layer-style`, `inherit-alpha` and `reference-hidden`
  have `fix` (how to fix it), and their `message` says only what is
  wrong.
- **Grouped results.** `by_rule[rule].fix` appears when every finding of
  the rule has the same fix; its items then leave it out.
- **Flat results** (`group_by: "none"`) keep `fix` on each finding.
- **`stray-alpha`.** The message is "Paint layer 'X' has 3 stray specks,
  40 px, e.g. at …". The definition of a speck and the
  `krita_trim_layers(…)` call are in `fix`.
