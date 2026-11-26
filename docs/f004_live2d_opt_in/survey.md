# F004 survey: which domain logic is Krita's, and which is Live2D's

Many of krita-mcp's lints, warnings and errors were written for one
workflow: preparing Live2D art under `live2d-workspace/CONVENTIONS.md`.
A Krita MCP doesn't have to enforce those conventions by default. This
survey sorts every piece of domain logic in the plugin and the MCP
server (F001–F004) into three categories, and says how each one is
switched.

| Category | Meaning | Switch |
|---|---|---|
| **1. Inherent** | Right for any agent working with Krita: safety, correctness, or the MCP's own mechanics (refs, the undo journal, manifests) | Always on. Some have value knobs (limits, allowed dirs) but no off switch |
| **2. Usual** | Usually relevant to anyone preparing layered art, but an agent can have a good reason not to want it | On by default. Opt out with `lint.disable` / `lint.reference_pattern: null` |
| **3. Convention** | Only relevant when following CONVENTIONS.md (Live2D / Cubism rigging) | Off by default. Opt in with `live2d.enabled: true`; knobs under `live2d` |

How it's built (decisions.md G2–G4):

- **Lint packs.** `kritamcp/lint/` is split into `engine.py` (registry,
  packs, sets, settings), `core.py` (category 2) and `live2d.py`
  (category 3).
- **Sets.** The sets `default`, `conventions` and `all` expand to the
  rules the config enables, and `rules_off` lists the rest.
- **Explicit ids.** A rule named by id always runs.
- **Convention warnings outside lint.** These check the same
  `live2d.enabled` flag.

## Lint rules

| Rule | Severity | Category | Why | Config |
|---|---|---|---|---|
| `duplicate-name` | warning; **error** with `live2d` | 2 | Bare-name refs to the copies are ambiguous (D4), and reports and diffs can't tell them apart. That holds for any document. But Krita allows duplicates and agents can use ids, so it's opt-out. With `live2d` on, CONVENTIONS § Naming makes it an error and the pose-suffix `suggested_names` apply | `lint.disable`. Severity and pose suggestions come from the `live2d` pack |
| `empty-layer` | warning | 2 | An empty paint layer is usually leftover, but a placeholder is legitimate | `lint.disable` |
| `out-of-canvas` | warning | 2 | Pixels past the canvas don't render and usually come from a move. Storing art off-canvas is legitimate | `lint.disable` |
| `stray-alpha` | warning | 2 | Isolated faint specks are almost always eraser leftovers. Textured or noise brushes are legitimate | `lint.disable`; `lint.stray_alpha_below`, `stray_alpha_distance`, `stray_speck_max_px` |
| `node-type` | error | 3 | "Paint layers and groups only" is the rig's import limit (CONVENTIONS § Layer types). Filter, fill, vector and file layers and masks are normal Krita | `live2d.enabled` |
| `layer-style` | error | 3 | Same: layer styles are fine in Krita, but the rig can't import them | `live2d.enabled` |
| `inherit-alpha` | error | 3 | Same: clipping is a core Krita feature that the rig can't import | `live2d.enabled` |
| `blend-mode` | error | 3 | Only Normal / Multiply / Add map to the legacy Cubism modes (moc3 < 5.3) | `live2d.enabled`; `live2d.allowed_blend_modes`, `live2d.additive_mode` |
| `side-token` | warning | 3 | ` L` / ` R` as separate words, the character's side (CONVENTIONS § Naming) | `live2d.enabled` |
| `color-space` | error | 3 | The rig needs 8-bit RGBA sRGB. 16-bit, linear or CMYK documents are normal Krita | `live2d.enabled` |
| `nesting-depth` | warning | 3 (`conventions` set) | At most 3 groups (CONVENTIONS § Structure) | `live2d.enabled`; `live2d.max_nesting` |
| `name-case` | info | 3 (`conventions` set) | English Title Case (CONVENTIONS § Naming) | `live2d.enabled` |
| `blend-suffix` | warning | 3 (`conventions` set) | ` Multiply` / ` Add` suffix (CONVENTIONS § Naming) | `live2d.enabled`; `live2d.additive_mode` |
| `pose-visibility` | warning | 3 (`conventions` set) | Pose A visible, the others hidden (CONVENTIONS § Structure) | `live2d.enabled`; `live2d.pose_pattern`, `live2d.default_pose` |
| `reference-hidden` | warning | 3 (`conventions` set) | Reference groups stay hidden (CONVENTIONS § Naming) | `live2d.enabled`; `lint.reference_pattern` |
| `reference-unbracketed` (new, K8) | warning | 3 (`conventions` set) | A hidden group that is neither bracketed nor a pose group looks like reference material without the marker | `live2d.enabled`; `lint.reference_pattern`, `live2d.pose_pattern` |
| `mask-suffix` | info | 3 (`conventions` set) | A clipping base that exists only to clip ends in ` Mask` (CONVENTIONS § Naming) | `live2d.enabled` |

Before F004, every category-3 rule in the `default` set ran for every
agent, and `duplicate-name` was always an error. The `conventions` set
was already opt-in per call, and it still is with the pack on: CONVENTIONS
users pass `rules: ["all"]`, as the smoke test does.

### Lint mechanics

| Behaviour | Category | Config |
|---|---|---|
| The rule engine, sets, `severity_min`, grouping, paging, sorting by stacking order | 1 | — |
| Pixel rules need an 8-bit RGBA document (`pixel_stats: false` otherwise) | 1 (an implementation limit of the alpha statistics) | — |
| Reference groups (and their contents) are skipped by every rule | 2. Excluding guide or reference material from lint is generally useful. The default marker (brackets) comes from CONVENTIONS but does no harm elsewhere (decisions.md G5) | `lint.reference_pattern` (a regular expression; `null` turns it off) |
| Pose groups: sibling groups `<X> A`, `<X> B` (umamo U11's definition) | 3 | `live2d.pose_pattern`, `live2d.default_pose` |
| Group and layer names are separate namespaces in `duplicate-name` (the Face group may hold the Face layer) | 2. CONVENTIONS spells it out, but it also matches what an agent needs: refs can always use a path | — |

## Warnings and errors outside lint

| Code (where) | Category | Why | Config |
|---|---|---|---|
| `forbidden_path` errors (all file tools) | 1 | Security: the allowlist | `allowed_dirs` (values only) |
| `too_large` (`new_canvas`, `set_canvas_size`) | 1 | Resource limit | `limits` (values only) |
| `ambiguous`, `not_found` (refs, D4) | 1 | The MCP's ref mechanics | — |
| `duplicate_name` (`update_layers`, `duplicate_layer`, `merge_layers`, `selection_to_layer`): same kind only since K9 | 1 | A bare-name ref to the node becomes ambiguous. That's about the MCP's refs, not a convention | — |
| `name_shared_across_kinds` note (same tools, new in K9) | 1 | Same, for a group/layer pair: allowed, but the bare name now matches both | — |
| `duplicate_names_in_copy` (`duplicate_layer`) | 1 | Same | — |
| `not_active_document` (`set_active_layer`) | 1 | Painting without `doc` targets the active document | — |
| `not_undoable`, `partial_ui_undo` (journal) | 1 | Undo-journal mechanics | `journal` (values only) |
| `masks_not_applied`, `not_exported`, `off_canvas`, `not_recreatable`, `too_many_layers` (`export_layers`); `document_size` (`create_layers` from a manifest) | 1 | Round-trip fidelity of the manifest format | — |
| `pass_through_flattened`, `not_adjacent`, `isolated_compositing`, `clipping_base_not_merged`, `hidden_layers_dropped`, `nothing_visible` (`merge_layers`) | 1 | What a merge changes in the render. "Bake clipping" is a Live2D motive, but the warnings are about Krita compositing | — |
| `empty_selection`, `nothing_copied` (selection tools) | 1 | A call that did nothing visible | — |
| `size_mismatch`, `not_rgba8` (`diff_documents`) | 1 | What the diff could compare | — |
| Unknown blend-mode ids refused (`blendmodes.py`) | 1 | Krita stores any string and renders an unknown id as Normal, silently | — |
| `add` ↔ `linear_dodge` conversion on `.psd` / `.ora` export (D17) | 1 | Works around Krita's exporters losing the Add mode | — |
| `render` / `get_canvas` cost hint | 1 | Performance advice; K10 drops it once `png_level` ≤ 1 | — |
| `rig_breaking` (`set_canvas_size`) | **3** | "Mesh coordinates are canvas pixels" is only true once a rig exists (CONVENTIONS § Canvas). For anyone else a canvas resize is routine | `live2d.enabled` |

## Tools that serve the workflow but aren't conventions

`occlusion_map` ("what does a moving part reveal"), `trim_layers`,
`layer_stats`, `transform_layer` (flip for mirrored parts), `merge_layers`
(baking clipping), `export_layers` / `create_layers` (manifests) and
`diff_documents` were built for the Live2D workflow. They're generic
capabilities, so they stay category 1 and need no switch. Where their
descriptions use Live2D as the example, the wording stays.

## CONVENTIONS.md items krita-mcp doesn't check

For completeness. None of these are enforced, so nothing changes:

- repository layout and Git
- the fixed canvas size (only the `rig_breaking` warning touches it)
- painting resolution
- effect sprites in their own groups
- stacking order at rest
- one movable piece per layer
- painting what moving parts reveal (`occlusion_map` helps)
- clean edges beyond stray specks
- overlaps at joints
- symmetry and mirrored parts
- rig names and IDs

## Your config

[config.md](config.md) has the `~/.krita-mcp-config.json` that keeps
everything in CONVENTIONS.md applying.
