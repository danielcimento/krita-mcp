# F004 tool contract changes

Deltas against F003's [api-changes.md](../f003_smoke_test_fixes/api-changes.md)
and F001's [api-design.md](../f001_buildout/api-design.md). The tool
docstrings in `server.py` are the full current contract. Protocol **3**
(unchanged, G9), plugin **0.4.0**.

## Config file (`~/.krita-mcp-config.json`)

| Key | Default | Meaning |
|---|---|---|
| `lint.reference_pattern` | `"^\\[.*\\]$"` | New. A group whose trimmed name matches it (`re.search`) is reference material, skipped with its contents by every rule; `null` for none (G5) |
| `lint.disable` | `[]` | New. Rule ids left out of the `default` / `conventions` / `all` sets |
| `lint.additive_mode` | — | **Moved** to `live2d.additive_mode`. Still read, with a `config_errors` note (G3) |
| `live2d.enabled` | `false` | New. Turns the Live2D conventions on: the `live2d` lint pack, `duplicate-name` as an error with pose suggestions, `set_canvas_size`'s `rig_breaking` |
| `live2d.additive_mode` | `"either"` | Was `lint.additive_mode` |
| `live2d.allowed_blend_modes` | `["normal", "multiply", "add", "linear_dodge"]` | New. Paint-layer blend modes `blend-mode` accepts (Krita ids) |
| `live2d.max_nesting` | `3` | New. `nesting-depth`'s limit (1–64) |
| `live2d.pose_pattern` | `"^(?P<stem>.*\\S) (?P<pose>[A-KM-QS-Z])$"` | New. A pose group's name, matched in full; needs the groups `stem` and `pose` |
| `live2d.default_pose` | `"A"` | New. The pose `pose-visibility` keeps visible |

## `krita_health` / `/health`, `krita_reload_config`

- **`lint`** now has `reference_pattern` and `disable`, and no
  `additive_mode`.
- **`live2d`** (new) is the resolved `live2d` section; `null` if the
  config couldn't load.

## `krita_lint` (command `lint`)

- **Rules.** New rule `reference-unbracketed` (warning, set
  `conventions`, pack `live2d`; K8, G6). Its findings for groups have
  `suggested_names: [{id, name}]`.
- **Packs.** Each rule is in pack `core` (`duplicate-name`, `empty-layer`,
  `out-of-canvas`, `stray-alpha`) or `live2d` (the other 13). The sets
  expand to the enabled rules only (G2). A rule named by id always runs.
- **`rules_off`** (new, always present): `{reason: [rule ids]}`, where the
  reason is `"live2d"` (the pack is off) or `"disabled"` (in
  `lint.disable`). The summary adds "N Live2D convention rules off (set
  live2d.enabled in the config to run them)" and "N rules disabled by
  lint.disable".
- **`duplicate-name`.** Severity is `warning`, `error` with the `live2d`
  pack on. `suggested_names` appears only with the pack on (G4).
- **`settings`** is now `{stray_alpha_below, stray_alpha_distance,
  stray_speck_max_px, reference_pattern, disable, live2d: {enabled,
  additive_mode, allowed_blend_modes, max_nesting, pose_pattern,
  default_pose}}`.
- **`blend-mode`'s additive message** names `live2d.additive_mode`.
- **Grouped items.** `by_rule[rule].items` leave out `rule` and
  `severity` (G11).
- **Registry order** (`rules_run`): core rules first, then Live2D.

## `krita_layer_stats`

`settings` holds the stray-alpha settings only (`additive_mode` is gone).

## `krita_set_canvas_size`

`rig_breaking` is in `warnings` only with `live2d.enabled`, and the
summary mentions rigs only when it is.

## `krita_update_layers`, `krita_duplicate_layer`, `krita_merge_layers`, `krita_selection_to_layer`

- **`duplicate_name` warnings** only for a node of the same kind (group
  and group, layer and layer). Its message says "like N other layer(s)" or
  "group(s)".
- **`notes`** (new; present only when non-empty): `[{code:
  "name_shared_across_kinds", id, name, others, message}]` for a name a
  node of the other kind has. That's allowed, and the bare name matches
  both. Not counted as a warning in the summary (K9, G7).

## `krita_render`, `krita_get_canvas`

- **`krita_render`.** The summary drops the full-size cost hint when
  `png_level` is 0 or 1 (K10).
- **`krita_get_canvas`.** The hint says "use krita_render with
  scale=0.25 for previews, or png_level=1", since `get_canvas` takes
  neither.
