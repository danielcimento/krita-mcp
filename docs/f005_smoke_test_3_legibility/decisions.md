# F005 decisions

Numbered H1… so they don't collide with F001's D1–D17, F003's E1–E9 or
F004's G1–G11. Status is **Accepted** unless marked otherwise.

## H1. Nothing folds into the `live2d` pack

**Context.** You asked for any new lint that only matters under
`live2d-workspace/CONVENTIONS.md` to go into the `live2d` pack, behind
`live2d.enabled`.

**Decision.** None of K11–K15 adds a lint rule. Applying F004's test
("would this be wrong advice for someone who isn't building a Cubism
rig?", G1), each change is general:

- **K11**: telling an agent that its paint landed under other layers
  helps anyone.
- **K12–K15**: result shapes and error text, category 1.

So no pack change and no new `live2d` knob. One thing about K11 *is*
rig-flavoured: rigging often paints covered areas on purpose (the
skin under the hair that shows when the head turns). That's why its
notes can be switched off (H3) rather than why they should be opt-in.

## H2. K11 `changed` is the pixels that changed, not the rectangle written

**Decision.** Each painting tool compares the layer's bytes before the
write (the journal snapshot it already took) with the bytes it wrote:

- **`changed`** is the bounds of the pixels that differ, `{x, y, w, h}`,
  or null when none did.
- **`changed_px`** counts them.

Repainting the same colour gives `changed: null` and a `nothing_changed`
note. `clear` reports exactly what it erased.

**Why not the written rectangle, as `import_png` does?** A stroke's
rectangle includes its soft margin, and an ellipse's includes its
corners. The pixels that changed are also what the coverage check
needs (H3). `import_png`'s `changed` stays the rectangle it wrote.

**Cost.** One XOR over the snapshot, in 1 Mpx strips (`changes.py`).
That's about 14 ms per Mpx that differs (strips that didn't change are
skipped), so up to about 0.7 s for a `clear` that changes all of Mao's
48.7 Mpx. A layer that isn't 8-bit RGBA gets the written rectangle, with
`changed_px` null.

## H3. K11 coverage: `occlusion_map`'s render, over the changed bounds only; `paint.check_visibility`

**Decision.** `covered_pct` uses `occlusion_map`'s own path
(`occlusionplan.plan` and `_coverage_render`) with its defaults: Normal
units drawn after the layer, threshold 0.5, rendered in a `clone()`. It
is restricted in two ways:

- **The rectangle** is the changed bounds clipped to the canvas, not the
  layer's bounds.
- **The units** are only those whose bounds overlap that rectangle. If
  none overlap, `covered_pct` is 0 with no render, which is the common
  case of painting on top.

`layer_visible` is the layer's own flag and every ancestor's. When it is
false there is no render, `covered_pct` is null, and the note says which
node hides it.

**Cost on Mao** (5800×8400, 222 nodes; Choker, 15×10 px): 173 ms with
the check, 74 ms without it.

**Config key.** `paint.check_visibility` (default `true`). `false`
skips the render, `covered_pct` (null), `top_cover` and every note.
`changed` and `layer_visible` stay. This follows your instruction to
make suppressible notes configurable. It's for:

- agents that paint covered areas on purpose (H1)
- agents that paint a lot and want the 100 ms back

It is a top-level `paint` section, not under `lint` or `live2d`,
because it changes a tool's result and not a rule set.

## H4. K11 numbers: `covered_pct` is a 0–1 share; the note needs every changed pixel covered

- **Scale.** `covered_pct` uses the scale of `occlusion_map`'s field of
  the same name, a share from 0 to 1. The work order's acceptance,
  "returns `covered_pct: 100`", is read as 100 %, which is `1.0`. Two
  fields with the same name and different scales would be a trap.
- **Notes**, one at most:
  - **`layer_hidden`**: the layer or a group above it is hidden.
  - **`change_covered`**: every changed pixel on the canvas is covered at
    the threshold (`covered_pct == 1`). When all of them are fully
    covered (coverage 255, `occlusion_map`'s `fully_hidden`) the text is
    the work order's: "this change isn't visible in the composite: 100%
    covered by layers above (top cover: '…')". Otherwise, under a veil
    at least half opaque, it says the change "hardly shows".
  - **`nothing_changed`**.

  A partly covered change gets no note: the number is there, and the
  work order asks for a note only when the change won't show.
- **`top_cover`** is `{id, path}` of the unit above whose own alpha (a
  paint layer's pixels, a group's projection) covers the most changed
  pixels at the threshold, the nearest on a tie. It is an attribution,
  not a second coverage render: two half-opaque layers can cover a
  pixel together while neither does alone, and then `top_cover` is
  absent. On Mao the Choker repaint names `Head/Face/Face Shadow`, the
  layer that covers that area (the work order's `Neck` was an example).

## H5. K12: `child_count` always, `children` on request; the 8 KB guard is server-side; 2 KB not reached

**Decision.**

- **Fields.** `children` is an ordinary layer-object field. `child_count`
  joins `id` and `name` as always present, so a row at a `depth` cut-off
  still says there is more below. The server's default fields lose
  `children`. `fields: ["all"]` and `path=` still include both.
- **Guard.** `krita_mcp_server/pages.fit_page` runs on every
  `list_layers` page in `server.py`:
  - **The cut.** A page whose JSON would pass 7.5 KB keeps the first
    rows that fit. `next_offset` moves to the first row left out, and
    `trimmed: {rows, of, max_bytes, dropped_fields}` and the summary
    say so.
  - **Dropped fields.** `children` is dropped only when not even one
    row fits, and is named in `dropped_fields`. Fields the caller asked
    for are otherwise kept, and shortening the page loses nothing that
    the next page doesn't return.
  - **Why server-side.** The budget is about the MCP result, the server
    already shapes it (the summary), and it then works with an older
    plugin too. It is pure, so the headless smoke replay applies the
    same function.

**Results on Mao** (`work.kra`'s tree):

| Call | F004 (run's estimate) | F005 |
|---|---|---|
| `list_layers(depth: 1)` | about 6.5 KB | 2.76 KB |
| `list_layers(root: "Head", fields: [path, bounds, visible], limit: 100)` | about 13 KB | 7.46 KB, cut to 35 of 62 rows, `next_offset` 35 |
| `list_layers()` (defaults, 30 rows) | — | 5.1 KB |

**Not met: "`list_layers(depth: 1)` is under 2 KB".** The 16 rows alone
are 2,455 bytes, about 153 bytes each. The id alone is 46 bytes of
that. Getting under 2 KB would mean dropping default fields as well as
`children`:

- `type`, `visible` and `blend_mode`
- or leaving out default values (no `visible: true`, no
  `blend_mode: "normal"`), which isn't enough on its own (about 2.1 KB)
  and makes a missing key mean something

Both change what every agent gets from the default listing, beyond the
work order's "children is an ordinary field". I kept the rows explicit
and report the gap instead. It's yours to decide:

- **Option A.** Drop `blend_mode` and `visible` from the defaults (the
  `overview` already lists the hidden and non-Normal layers, up to 20
  each). That gives about 2.1 KB.
- **Option B.** Change the acceptance to under 3 KB.

## H6. K13: who gets suggested

`refs.resolve` adds the suggestions to `not_found` for a bare name or a
name path (not for a UUID or a `$key`). The message ends "; did you mean
'Hat/Hat Top', …?", since the message is what the agent sees, and
`details.suggestions` is `[{id, path}]`. Up to 5:

1. **Same group first.** The children of the group where the path
   stopped matching (the top level when its first name didn't match),
   ranked by name similarity to the name that failed. Every child
   counts, so a small group lists what it holds.
2. **Then anywhere.** Layers anywhere else whose name is similar to the
   ref's last name, at a score of 0.6 or more.

Similarity is `difflib`'s ratio on case-folded names, raised to 0.8 when
one name is a prefix or a whole word of the other (`Hat` and `Hat Top`)
and to 0.95 for a case-only difference. The path-string similarity I
tried first suggested `Head` for `Hat/Hat`, so it was dropped. On Mao,
`occlusion_map("Hat/Hat")` gives "did you mean 'Hat/Hat Top', 'Hat/Hat
Brim', 'Hat/Hat Shadow Multiply', 'Hat/Rabbit', 'Hat/Feather 1'?".

## H7. K14: findings carry `fix`; the group gives it once

**Decision.** `finding(..., fix=…)` stores the fix as its own key, and
`message` then says only what is wrong.

- **Grouped results.** `group_findings` hoists `fix` into the group when
  every finding of the rule has the same one, and the items leave it
  out.
- **Flat results** (`group_by: "none"`). Findings keep `fix` as a key
  rather than in `message`.
- **Rules converted**, the ones whose fix text never depends on the
  node:
  - `stray-alpha`. Its fix also carries the definition of a speck,
    which every item repeated too. Items keep the layer, the speck
    count, `pixels`, `bounds` and the sample rectangles.
  - `empty-layer`, `out-of-canvas`, `layer-style`, `inherit-alpha` and
    `reference-hidden`.
- **Rules left alone.** Rules whose fix names the node (renames, blend
  modes, side tokens, `reference-unbracketed`'s two readings) keep it
  in `message`.

**Result on Mao.** The default `lint(rules: ["all"])` drops from 7.35 KB
(F004) to 6.65 KB.

**Contract change.** A flat finding's `message` no longer ends with the
fix for those six rules. Agents that read only `message` should read
`fix` too. The `krita_lint` docstring says so.

## H8. K15 and versions: server 0.5.0 from one checkout, plugin 0.5.0, protocol 3

- **`krita_mcp_server/version.py`.** `SERVER_VERSION`, kept equal to the
  plugin's `PLUGIN_VERSION` by a unit test, since both ship from one
  checkout. `server_commit` is the checkout's HEAD and `server_dirty`
  is `git status --porcelain --untracked-files=no` non-empty, so
  untracked scratch files don't count. Both are read once per server
  process, which is the code that's loaded, and are null outside a git
  checkout. `krita_health` returns them even when Krita is down, and
  the summary ends "server 0.5.0 (commit a4fe3ce2, dirty)".
- **Plugin 0.5.0.** The work order says 0.4.0, which the run already
  used. The default shapes changed (rows without `children`, lint
  messages without their fix), which F004 G9 treats as a minor bump.
- **Protocol 3.** A 0.5.0 server asks for nothing a 0.4.0 plugin
  lacks: `child_count` comes from the plugin unasked, and the server's
  new summaries read optional fields. Checked live: the new server
  against the running 0.4.0 plugin passed 20 live tests (27 with the
  Mao acceptance tests). Only the three that need 0.5.0 failed, saying
  to restart Krita.
