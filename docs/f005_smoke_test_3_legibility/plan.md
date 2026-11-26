# F005 plan: smoke test revision 3 follow-ups (legibility)

| | |
|---|---|
| Request | [spec.md](spec.md) (work order "Spec v4", K11–K15). Your additions: fold any CONVENTIONS-only lint into the `live2d` pack, and give the new warnings an agent may want to suppress a config key (as F004 did) |
| Decisions | [decisions.md](decisions.md) (H1–H8) |
| Tool contract changes | [api-changes.md](api-changes.md) |
| Evidence | Smoke test 2026-11-26T12-07-02 (revision 3), `~/smoke-out/2026-11-26T12-07-02/REPORT.md` (untracked) |
| Base | `main` @ a4fe3ce (plugin 0.4.0, protocol 3) |
| Status | **Done** except two things that are yours: the live F005 tests need Krita restarted on this checkout's plugin (0.5.0), and K12's "under 2 KB" isn't met (2.76 KB; H5 has the options) |

## Scope

- **The work order:** K11 (painting tools report the changed rect, the
  layer's effective visibility, the share of the change covered from
  above, and a note when it won't show), K12 (lean `list_layers` rows,
  `child_count`, an 8 KB guard), K13 ("did you mean" on `not_found`
  layer refs), K14 (lint fix hints once per rule group), K15
  (`krita_health` reports the server's version and commit), with the
  regression checks kept.
- **Your additions:**
  - Live2D folding: none of K11–K15 adds a lint rule or a
    CONVENTIONS-only behaviour, so nothing moves into the `live2d` pack
    (H1).
  - Config keys: the painting tools' visibility check gets
    `paint.check_visibility` (H3). K12–K15 add no warning to suppress.
- **Out of scope:** the umamo findings in the same report.

## Tasks

Status: ☐ open · ◐ in progress · ☑ done.

| # | Task | Spec | Status |
|---|---|---|---|
| T1 | `stroke`, `fill`, `draw_shape`, `clear`: `changed` (tight bounds of the pixels that changed), `changed_px`, `layer_visible`, `covered_pct` (occlusion_map's coverage render over the changed rect), `top_cover`, `notes` | K11 | ☑ H2–H4 |
| T2 | Config `paint.check_visibility` (default true): off skips the coverage render and the notes | yours | ☑ H3 |
| T3 | `list_layers`: `children` an ordinary field, `child_count` always; default fields lose `children`; the server's 8 KB page guard with `next_offset` | K12 | ☑ H5 (2 KB not met) |
| T4 | `not_found` for a layer name or path lists up to 5 nearest layers (same parent first, then name similarity) with full paths | K13 | ☑ H6 |
| T5 | Lint: a rule's shared fix hint goes once into the group (`fix`); `stray-alpha` items keep the numbers | K14 | ☑ H7 |
| T6 | `krita_health`: `server_version`, `server_commit`, `server_dirty` | K15 | ☑ H8 |
| T7 | Docstrings, README, architecture, plugin and server 0.5.0 | all | ☑ |
| T8 | Tests: unit, headless (`test_paint_report`, updated shapes), smoke replay (K11 Choker, K12 sizes, K13, K14), live (`test_f005`; the live `Caller` follows `next_offset`) | all | ☑ |

## Results

Measured 2026-11-26 on `mao_pro_layered.kra` (the run's fixture) through
the smoke replay (`test_smoke_sizes`, Live2D conventions on), after the
v3 sequence (renames, Stage 4, Stage 6):

| Work order acceptance | Result | Test |
|---|---|---|
| K11: the Choker repaint returns `covered_pct: 100` and the note in the same call | `covered_pct: 1.0` (the 0–1 share, H4), `layer_visible: true`, note `change_covered` "this change isn't visible in the composite: 100% covered by layers above (top cover: 'Head/Face/Face Shadow')". The Hat Top repaint: below 1, no note | `test_smoke_sizes`, `test_paint_report` (10 cases), live `test_f005` |
| K12: `list_layers(depth: 1)` under 2 KB | **Not met: 2.76 KB** (about 6.5 KB before). No `children`, `child_count` on every row. The 16 rows alone are 2.45 KB (H5) | `test_smoke_sizes` (bound 3 KB) |
| K12: `list_layers(root: "Head", limit: 100)` under 8 KB | 7.46 KB: 35 of 62 rows, `next_offset` 35, `trimmed` says so (about 13 KB before) | `test_smoke_sizes`, `test_server_tools` |
| K13: `occlusion_map("Hat/Hat")` suggests the right layer | "did you mean 'Hat/Hat Top', 'Hat/Hat Brim', 'Hat/Hat Shadow Multiply', 'Hat/Rabbit', 'Hat/Feather 1'?" | `test_smoke_sizes`, `test_refs`, live `test_f005` |
| K14: fix hint once per rule | `by_rule["stray-alpha"].fix` holds the definition and the `krita_trim_layers(…)` call; items are `{layer_id, path, message, pixels, bounds}`. Default `lint(all)` 6.65 KB (7.35 KB in F004) | `test_smoke_sizes`, `test_f003_tools`, `test_lint` |
| K15: server version and commit | `server_version` 0.5.0, `server_commit` (full SHA), `server_dirty`, also when Krita is down | `test_server_tools`, live `test_f005` (passes on the 0.4.0 plugin too) |
| Regression: `reference-unbracketed` flags exactly `Expression References` | Unchanged (1 finding, before and after the B poses are hidden) | `test_smoke_sizes`, `MaoLintTest` |
| Regression: no `update_layers` warning for a name shared with its group | `warnings: []`, `Wand A` / `Wand B` notes | `test_smoke_sizes` |
| Regression: `forbidden_path`, named-fix errors, `suggested_names` | Unchanged; 11 `duplicate-name` pairs with `suggested_names` | `test_smoke_sizes`, `test_paths` |
| Regression: results ≤ 8 KB | Largest: `list_layers(root: Head, limit: 100)` 7.46 KB (guarded), default `lint(all)` 6.65 KB, `list_layers` 5.1 KB | `test_smoke_sizes` |

Cost of K11 on Mao (`draw_shape`, Choker): 173 ms with the check, 74 ms
with `paint.check_visibility: false`.

Suites:

- **`make test`**: 1400 passed.
- **`make test-krita`** with `KRITA_MCP_MAO_ORA` and `KRITA_MCP_MAO_KRA`
  set: 366 run, 0 failures, 0 skipped. kritarunner still aborts at exit
  after the result marker (F003's known issue, exit code 134).
- **`make test-live`** against your running Krita, which still has plugin
  0.4.0 loaded: 20 passed, 6 skipped. With `KRITA_MCP_MAO_ORA` set,
  `test_acceptance` is 7 passed, 3 skipped (your config file is a
  symlink, so the config-override tests skip). That is the new server
  against the old plugin, which shows protocol 3 still holds. The three
  plugin-side `test_f005` tests fail with "restart Krita so it loads this
  checkout's plugin (0.5.0)", as intended.

## Test plan

- `make test`: unit tests (refs suggestions, changes helpers, lint
  grouping, config, server wrappers and the list guard).
- `make test-krita`: headless suite; the Mao tests need
  `KRITA_MCP_MAO_ORA` and `KRITA_MCP_MAO_KRA`.
- `make test-live`: needs Krita restarted on this checkout's plugin.

## Risks

- **Agents that read only `message`** of a flat lint finding lose the fix
  for six rules: it's in `fix` now (H7). Grouped results, the default,
  give it once per rule.
- **Pages cut by the guard.** A caller that asked for 100 rows and reads
  only `layers` gets fewer. `next_offset`, `trimmed` and the summary say
  so. The live harness follows `next_offset` for its whole-tree calls.
- **The coverage check's cost** grows with the area of the change and
  the layers over it. A big fill under many layers renders a big
  region. `paint.check_visibility: false` turns it off.
- **`top_cover` is an attribution** from each layer's own alpha, not a
  render. With several partly transparent covers it can be absent while
  `covered_pct` is 1 (H4).
