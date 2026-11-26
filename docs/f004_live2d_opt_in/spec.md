<!-- The work order as received, verbatim: live2d-workspace @ e16adf1,
docs/specs/krita-mcp-extensions-v3.md. Relative links point into that repo.
What was built, and where it departs from this, is in plan.md and decisions.md. -->

# Spec v3: krita-mcp follow-ups after smoke test revision 2

Status: draft · Target: `kritamcp` 0.3.0, protocol 3
Builds on: [v1](krita-mcp-extensions.md), [v2](krita-mcp-extensions-v2.md)
Evidence: smoke test run 2026-11-26, report at `~/smoke-out/2026-11-26/REPORT.md`

Every v2 item passed: lossless round trip, allowlist errors that name the
fix, results under 8 KB, quantified diffs, one-call rebuilds, and correct
duplicate-name scope. This spec is small.

## Changes

### K8 · Lint flags hidden groups that look like unbracketed reference material — P1

**Problem.** The fixture's `Expression References` group is hidden
reference material without brackets, which breaks CONVENTIONS ("Reference
material"). All 16 lint rules passed it, and that let a downstream umamo
import keep it as a rig part. See
[umamo v3](umamo-mcp-extensions-v3.md#the-failed-step-52-is-mostly-the-tests-fault)
for the full diagnosis.

**Required.** Add the rule `reference-unbracketed` (**warning**, not
error):
- **What it flags:** a hidden group whose name isn't in brackets and that
  isn't a pose-variant group.
- **Pose-variant group:** sibling groups whose names differ only in a
  trailing ` A`, ` B`, …, the same definition as umamo U11.
- **Why only a warning:** hidden rig parts that start hidden are legitimate.
- **Message:** offers both readings, plus `suggested_names`:
  "hidden group 'Expression References' isn't bracketed: if it's reference
  material, rename it to `[ Expression References ]`; if it's a rig part
  that starts hidden, ignore this warning."
- **Hidden layers:** also apply it to hidden paint layers at top level (not
  inside a group).
- **Rule list:** include it in `rules: ["all"]`.

**Acceptance.** `lint` on `mao_pro_layered.kra` gives exactly one
`reference-unbracketed` warning, for `Expression References`. Neither
`[ Guide Image ]` nor the hidden `Arm L B` / `Arm R B` groups (after
Stage 5.1) are flagged.

### K9 · `update_layers` name warnings match the lint scope — P2

**Problem.** Applying lint's own `suggested_names` (`Wand A`, `Wand B`)
made `update_layers` warn about `duplicate_name`, because each matched
its parent group. That's allowed by the conventions and by K6.

**Required.**
- **Same kind only:** only warn when a new name clashes with a node of
  the same kind (paint layer against paint layer, group against group).
- **Cross-kind matches:** report a group/layer name match as an info
  note at most ("matches group 'Wand A' — allowed").

### K10 · Polish — P2

- **`render` hint:** the summary always suggests `png_level=1`. Drop the
  hint when that's already the level in use.
- **`krita_health` field name:** it reports the config location as
  `config_file`. That's fine. The answer key now uses that name, so no
  change is needed unless you want `config_path` as an alias.

## Regression checks (keep passing)

Everything in v2's list, plus:
- `create_layers(manifest, new_document: true)` rebuilding losslessly in
  one call
- the `forbidden_path` error being enough to fix the config
- `lint` reporting exactly 11 `duplicate-name` pairs with
  `suggested_names`
