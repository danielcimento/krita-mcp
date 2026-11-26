<!-- The work order as received, verbatim: live2d-workspace @ bf58017,
docs/specs/krita-mcp-extensions-v4.md. Relative links point into that repo.
What was built, and where it departs from this, is in plan.md and decisions.md. -->

# Spec v4: krita-mcp follow-ups after smoke test revision 3

Status: draft · Target: `kritamcp` 0.4.0, protocol 3
Builds on: [v1](krita-mcp-extensions.md), [v2](krita-mcp-extensions-v2.md), [v3](krita-mcp-extensions-v3.md)
Evidence: smoke test run 2026-11-26T12-07-02, report at `~/smoke-out/2026-11-26T12-07-02/REPORT.md`

## Where v3 landed

The run **passed**: every krita-mcp step was found on the first try, and
no default result was over 8 KB. The `reference-unbracketed` warning
caught `Expression References` without flagging the pose groups, and
lint's `suggested_names` fixed all 22 duplicates in one batch.

Everything below is cheap legibility work. Nothing gates.

## Changes

### K11 · Painting tools say what changed and whether it shows — P1

**Problem.** `draw_shape`, `stroke` and `fill` return only "Drew
rectangle at (x, y)". The agent's first Stage 6 repaint went into
`Choker`, which is 100% covered at rest, and it took 6 calls (render,
`get_color_at`, `occlusion_map`, two undos) to find out why nothing
showed.

**Required.** Painting tools return:
- `changed: {x, y, w, h}`, as `import_png` already does
- `layer_visible`: the layer's effective visibility, including whether any
  parent group is hidden
- `covered_pct`: how much of the changed area is covered by visible layers
  above, computed for the changed area only, the same way `occlusion_map`
  does it

When the change won't show, add a note: "this change isn't visible in
the composite: 100% covered by layers above (top cover: `Neck`)", or
"…layer is hidden".

**Acceptance.** Repeating the run's `Choker` repaint returns
`covered_pct: 100` and the note, in the same call.

### K12 · Lean `list_layers` rows — P1

**Problem.** `children` (child UUIDs) is always included, even when
`fields` doesn't ask for it and below a `depth` cutoff. The 16-row
top-level listing was about 6.5 KB, mostly UUIDs, and `root: "Head",
limit: 100` was about 13 KB.

**Required.**
- **Optional children:** `children` is an ordinary field. Leave it out
  unless asked for, and return `child_count` instead.
- **Size guard:** when a page would pass about 8 KB, shrink the rows (drop
  optional fields, say which) or shorten the page, and return
  `next_offset`. That's how umamo's `get_model_tree` already does it.

**Acceptance.** `list_layers(depth: 1)` on `work.kra` is under 2 KB,
and `list_layers(root: "Head", limit: 100)` stays under 8 KB.

### K13 · "Did you mean" for `not_found` — P1

**Problem.** `occlusion_map("Hat/Hat")` → "No layer at path 'Hat/Hat'",
with no suggestions. The agent needed a `list_layers` call to find
`Hat/Hat Top`.

**Required.** Every `not_found` for a layer path or name lists up to 5
of the nearest matches. Rank layers in the same parent group first,
then by name similarity, and include each match's full path.

### K14 · Lint fix hints once per rule — P2

**Problem.** Each of the 33 `stray-alpha` items repeats the same "erase
them, or clear them with `krita_trim_layers(…)`" sentence. The default
`lint(all)` was about 7.5 KB, close to the cap.

**Required.** Put the fix hint once in the rule group (`fix`), and keep
each item to its numbers: layer, pixel count, bounds.

### K15 · Report the server's own version — P2

`krita_health` reports the plugin version, protocol and Krita build,
but not the MCP server's version or commit (the report says "no server
commit reported"). Add `server_version` and `server_commit`, with a
dirty flag, as umamo's `server_info` does.

## Regression checks (keep passing)

Everything in v3's list, plus:
- `reference-unbracketed` flags exactly `Expression References` (no pose
  groups, no `[ … ]` groups)
- `update_layers` doesn't warn when a layer shares a name with its group
- `forbidden_path`, the `pack_atlas`-style named-fix errors, and lint
  `suggested_names`, which the run singled out as good examples
