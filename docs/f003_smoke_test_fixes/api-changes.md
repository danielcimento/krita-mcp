# F003 tool contract changes

Deltas against [../f001_buildout/api-design.md](../f001_buildout/api-design.md).
The tool docstrings in `server.py` are the full current contract.
Protocol **3**, plugin **0.3.0**.

"Tool default" is what the MCP tool sends when the caller leaves an
argument out. "Command default" is what the plugin command does when the
parameter is absent (F001 behaviour, kept for internal callers; E8).

## Manifest (v1, extended)

| Field | Where | Meaning |
|---|---|---|
| `device_origin` `{x, y}` | paint entries | `Node.position()` of the source layer: where its 64 px tile grid starts. Optional; `create_layers` applies it before writing pixels (E1). Older manifests have none, which means (0, 0) |
| `x, y, w, h` | paint entries | The PNG's rectangle. Since F003 it can be larger than the layer's alpha bounds by alpha-0 pixels that carry colour (E2) |
| `document` | top level | Unchanged (name, width, height, resolution, color_model, color_depth, color_profile); now read by `create_layers(new_document=true)` |

## `krita_health` / `/health`

New fields: `config_exists`, `config_key` (`"allowed_dirs"`),
`config_dirs` (the file's entries as written), `allowed_dirs_env`
(`"KRITA_MCP_ALLOWED_DIRS"`), `config_reload` (how changes take effect).
The summary lists the allowed dirs and where to add one.

## `krita_reload_config` (new; command `reload_config`)

Re-reads the config file and returns the same config fields as `/health`.
Not mutating.

## `forbidden_path` errors

Outside the allowed roots, the message has these lines:

```
<path> is outside the allowed directories[ (it resolves to <realpath>)]
  allowed: <root>, <root>, …
  to allow it: add the folder "<dir>" in "allowed_dirs" in <config file>, e.g. {"allowed_dirs": [...]}; it applies from the next call, no restart needed (krita_reload_config shows the result)
  [note: <dir> does not exist yet; …]
  [config problems: allowed root '…' skipped: …]
```

`details`: `path`, `resolved`, `allowed_dirs`, `config_file`,
`config_key`, `config_exists`, `suggested_dir` (null when only home or
above would do). When the file doesn't exist, the line says "create the
file with" instead. A disallowed extension says which extensions are
allowed (`details.allowed_extensions`).

## `krita_new_canvas`

`background: "none"`: no layer at all; `background_layer` is null.

## `krita_create_layers`

| Parameter | Tool default | Command default | |
|---|---|---|---|
| `new_document` | false | false | Manifest only, instead of `doc`: create the document from the manifest's `document` block, with no layer, and build into it. Result adds `new_document {name, width, height, resolution, color_profile}`; `doc` is the new handle |
| `name` | — | — | The new document's name (`new_document` only) |
| `verbose` | `"auto"` | `true` | `created` listed in full (`true`), never (`false`, `created_listed: false`), or when at most 20 (`"auto"`). Dry runs: `would_create` cut to the first 20 the same way, with `would_create_listed` |
| spec `device_origin` | — | — | Paint layers: `{x, y}`, see Manifest |

## `krita_export_layers`

| Parameter | Tool default | Command default | |
|---|---|---|---|
| `verbose` | false | true | `layers` (the manifest entries) in the result |

The result always has `counts {paint, group, projection, png, skipped}`.
PNGs are straight RGBA, cropped to content (E2).

## `krita_list_layers`

| Parameter | Tool default | Command default | |
|---|---|---|---|
| `fields` | id, name, type, visible, blend_mode, children | all | `["all"]` in the tool means every field |
| `limit` / `offset` | 30 / 0 | none / 0 | Result adds `offset`, `limit`, `next_offset` (null at the end) when paging |
| `overview` | true | false | Adds `overview {counts, max_depth, hidden {count, paths}, blend_modes {mode: {count, paths}}}` (paths capped at 20) |
| `summary_only` | false | false | Only `counts` and `overview` |
| `path` | — | — | Write `{doc, root, layers (every field), counts, overview}` to a `.json` file inside an allowed dir; returns `path`, `bytes`, `counts`, `overview` |

`counts` always covers the whole listing, not just the page.

## `krita_lint`

| Parameter | Tool default | Command default | |
|---|---|---|---|
| `group_by` | `"rule"` | `"none"` | `"rule"`: `by_rule {rule: {severity, count, items, next_offset}}` instead of `findings` |
| `limit` / `offset` | 5 / 0 | none / 0 | Per rule with `"rule"`, over all findings with `"none"` |

The result adds `total` (all findings after `severity_min`). Findings:

- `duplicate-name`: separate namespaces for groups and layers; message
  `Name 'X' is used by N paint layers|layers|groups ('a/X', 'b/X'); … must be unique (a group may share one): <fix>`;
  `suggested_names: [{id, name}]` when the copies sit in different pose
  groups.
- `stray-alpha`: `pixels` (speck pixel count) and `bounds` (box around
  every speck) on the finding; the message names the box and the
  `krita_trim_layers` call that would clear them. The snapshot's
  `stray_specks` (and `layer_stats`) gain `bounds`.

## `krita_diff_documents`

| Parameter | Default | |
|---|---|---|
| `ignore_alpha0_rgb` | true | Alpha-0-only colour differences don't set `projection_changed` or list a layer in `pixel_changes` |

- `pixel_changes[]` adds `max_channel_diff`, `alpha0_rgb_count` and
  `alpha0_rgb_bounds`. Layers are compared over their tile windows.
- New `projection {changed_count, bounds, max_channel_diff,
  alpha0_rgb_count, alpha0_rgb_bounds}`; `projection_changed` stays.
- `counts` adds `alpha0_rgb_only_layers`.
- The result echoes `ignore_alpha0_rgb`.

## `krita_stroke`, `krita_fill`, `krita_draw_shape`

Optional `color` (hex) for this call only; the stored colour
(`krita_set_color`) is unchanged. Results include `color`, the colour
used.
