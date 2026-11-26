# F004 decisions

Numbered G1… so they don't collide with F001's D1–D17 or F003's E1–E9.
Status is **Accepted** unless marked otherwise.

## G1. Three categories, with the Live2D conventions opt-in

**Context.** You asked for a survey of krita-mcp's domain logic in three
categories:

- always applicable (inherent)
- usually relevant (opt-out)
- only relevant under `live2d-workspace/CONVENTIONS.md` (opt-in, with
  knobs)

You also asked for it to be modularised to match.

**Decision.** [survey.md](survey.md) sorts every lint rule, warning and
error. The test for category 3 is "would this be wrong advice for
someone using Krita who isn't building a Cubism rig?":

- **Category 3.** A filter layer, a layer style, clipping, a screen
  blend, a 16-bit document, `Eye_L`, a resized canvas: each is normal
  Krita.
- **Category 2.** Duplicate names, empty and off-canvas layers, and stray
  specks are hygiene for anyone. Each still has a legitimate exception.
- **Category 1.** The rest of the warnings come from the MCP's own
  mechanics: refs, the undo journal, the manifest format, compositing
  side effects, and security.

**Consequences.** No lint rule is category 1. Lint is advisory, so
everything in it has an off switch. Category 1 is where the warnings
the tools raise on their own belong.

## G2. Lint packs; sets expand to the enabled rules; ids always run

**Decision.** `kritamcp/lint.py` becomes a package:

- `engine.py`: the registry, packs, sets, settings and `run`.
- `core.py`: the category 2 rules.
- `live2d.py`: the category 3 rules and their helpers (side tokens,
  Title Case, pose groups).

A `Pack` declares:

- its settings (under `settings[<name>]`, with `enabled`)
- severity overrides while it's on
- a `duplicate-name` rename suggester
- a settings check

Every rule belongs to `core` or a pack. The sets `default`,
`conventions` and `all` keep their meaning and expand to the rules whose
pack is on and that aren't in `lint.disable`. `run` returns `rules_off`
(`{"live2d": [...], "disabled": [...]}`) for what a requested set left
out, and the tool summary says how to turn them on. A rule named by id
always runs: an agent that asks for `pose-visibility` explicitly has made
its choice, and the pack's settings still apply.

**Why not refuse an id whose pack is off?** An agent that names a rule
would only have to add a config entry to get an answer it already
asked for. The config controls the defaults, not what can be asked.

**Consequences.** The registry order is core first, then live2d, so
`rules_run` is in a different order than in F003. Findings are sorted by
stacking order, so the order of findings didn't change. A future
convention set (another rig format) is a new pack module, imported from
`lint/__init__.py`, with its own config section.

## G3. `live2d` is a top-level config section

**Context.** Where do the switch and the knobs live?

**Decision.** A top-level `live2d` section with `enabled` (default
`false`) and the pack's knobs:

- `additive_mode`
- `allowed_blend_modes`
- `max_nesting`
- `pose_pattern`
- `default_pose`

It isn't under `lint`, because it also gates behaviour outside lint
(`set_canvas_size`'s `rig_breaking`). The generic lint settings stay
under `lint`, joined by `disable` and `reference_pattern`. Everything is
validated in `config.py`: a bad value keeps its default and says why in
`config_errors`. The regex knobs are compiled, `pose_pattern` must have
the `stem` and `pose` groups, blend modes must be Krita ids, and
`disable` must name known rules. The lint engine validates a snapshot's
settings again (`bad_request`), because snapshots can be hand-made.

**Legacy key.** F003's `lint.additive_mode` was always a Live2D setting.
It is still read and moved to `live2d.additive_mode`, with a note in
`config_errors`. If both are given, the new place wins.

## G4. `duplicate-name` is a warning in core and an error with `live2d`

**Decision.** Duplicate names stay in the core pack: a bare-name ref to
them is ambiguous for any agent (D4). Krita allows them, and agents can
use ids or paths, so in core the rule is a warning. The `live2d` pack
raises it to an error, as CONVENTIONS § Naming requires. Pose-suffix
`suggested_names` come from the pack's rename suggester, so they only
appear with the pack on. Without it the message suggests an index, as
text. Severity changes apply before `severity_min`, so
`severity_min: "error"` drops the rule when it's a warning.

## G5. The reference-group marker is a core knob, with brackets by default

**Context.** "Skip bracketed groups" comes from CONVENTIONS, but excluding
guides and reference sketches from lint is useful to anyone.

**Decision.** `lint.reference_pattern` is a regular expression, matched
with `re.search` against the trimmed group name. The default is
`^\[.*\]$`, F001's rule; `null` turns it off. It's category 2: a
non-Live2D user who names a group `[ Draft ]` loses only lint coverage
of that group, and can switch it off. The Live2D rules `reference-hidden`
and `reference-unbracketed` use the same pattern, so a user with a
different marker changes it in one place.

## G6. K8 `reference-unbracketed`: definition details

- **Hidden** means the node's own `visible` flag. Inheriting visibility
  from a parent doesn't count (pose B's children aren't flagged), and
  nothing inside a flagged group is flagged again. Mao gives one finding,
  not thirteen.
- **Pose group** follows umamo U11: sibling groups whose names match
  `live2d.pose_pattern` with the same stem, at least two of them. A lone
  hidden `Hat B` with no `Hat A` sibling is flagged. Cousins such as
  `Arm R A/Wand A` and `Arm R B/Wand B` aren't siblings, but the rule
  only looks at hidden nodes, and `Wand B` is visible itself.
- **Top-level hidden paint layers** are checked too, with the pose
  exemption among sibling paint layers. Brackets on a paint layer don't
  exempt it. CONVENTIONS and umamo U11 define reference material as a
  *group* in brackets, so the fix for a layer is to move it into one. The
  message says that, and the finding has no `suggested_names`.
- **`suggested_names`** for a group is `[ <name> ]`. It's only offered
  when that name matches `reference_pattern`, so a custom marker never
  gets a suggestion its own rule would reject.
- **Set:** `conventions`, next to `reference-hidden`. The work order asks
  for it under `rules: ["all"]`, and it's a warning about naming, like the
  other opt-in `conventions` rules.

## G7. K9: name clashes warn within a kind, and across kinds a note

**Decision.** `name_clashes(index, …)` in `commands/layers.py` splits the
nodes that share a new name by kind (`name_kind`: group, or any other
layer type), the same namespaces as `duplicate-name`.

- **Same kind:** the `duplicate_name` warning, as before.
- **Other kind:** a `name_shared_across_kinds` entry in a new `notes`
  list, present only when non-empty. The message says the name is
  allowed and that a bare-name ref now matches both.

The work order covers `update_layers`. The same helper now runs for
`duplicate_layer`, `merge_layers` and `selection_to_layer`, so the four
tools agree. Notes aren't counted in the summary's warnings: the work
order says "an info note at most".

## G8. K10: the render hint; no `config_path` alias

- **Render.** `render`'s summary drops the "full-size PNG encoding is
  slow" hint when `png_level` 0 or 1 was used. At 2–9 the hint stays,
  since `png_level=1` would still help.
- **`get_canvas`.** It printed the same hint, telling the agent to pass
  `scale` and `png_level`, which `get_canvas` doesn't take. It now says
  "use krita_render with …".
- **`krita_health`.** It keeps `config_file`. The work order says the
  answer key now uses that name, and an alias would be one more field to
  explain.

## G9. Plugin 0.4.0, protocol stays 3

**Decision.** The plugin's default behaviour changed: the lint `default`
set, `duplicate-name` severity, and `rig_breaking`. That's worth a minor
version an agent can see in `krita_health`. Nothing on the wire changed
shape incompatibly:

- **Requests.** No new request parameters.
- **Additions.** `rules_off` and `notes` are optional additions.
- **Canvas summary.** The server looks for `rig_breaking` before saying
  "breaks any existing rig". So a 0.4.0 server with a 0.3.0 plugin, or the
  other way round, still works. The protocol stays 3.

## G10. Tests choose their convention set explicitly

**Decision.**

- **Unit tests** (`tests/unit/test_lint.py`). `snap()` loads a snapshot
  with the Live2D pack on, since most rules are written for it, and
  `generic()` loads it as stored. A § Packs section tests the
  difference.
- **Headless suite.** It runs on the default config (the runner points
  `KRITA_MCP_CONFIG_FILE` at a missing file). Tests of conventions turn
  them on with `temp_config({"live2d": {"enabled": True}})`. The smoke
  replay (`test_smoke_sizes`) and the Mao lint test do too, since they
  replay a CONVENTIONS user.
- **Live tests** run against your config. They read `live2d` from
  `/health`: acceptance tests of the conventions fail and name the
  config key when it's off, and the canvas test expects `rig_breaking`
  exactly when it's on. A plugin from before F004 has no `live2d` field,
  and counts as on.

## G11. Grouped lint items drop the keys their group gives

**Found while running the smoke replay.** With the Live2D pack on, the
smoke test's `lint(rules: ["all"])` on Mao grew from F003's 7.1 KB to
7,971 bytes. That's over the 8 KB budget (K3, a v2 regression check)
once the server's summary is added. Two things caused it: K8's new
`reference-unbracketed` group, and the larger `settings` echo (the
`live2d` block).

**Decision.** In `group_by: "rule"` (the tool default) each item leaves
out `rule` and `severity`, which its group already gives. That cuts about
45 bytes per item, and the result is now 7,350 bytes. `group_by: "none"`
keeps whole findings. Nothing read those keys from grouped items. The
alternatives were dropping the settings echo, or a smaller default
`limit`. Either would have removed information, not repetition.
