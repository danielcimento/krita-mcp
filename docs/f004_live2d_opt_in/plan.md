# F004 plan: smoke test revision 2 follow-ups, Live2D conventions opt-in

| | |
|---|---|
| Request | [spec.md](spec.md) (work order "Spec v3", K8–K10). Your addition: survey the domain logic into three categories and make the CONVENTIONS-only parts opt-in |
| Decisions | [decisions.md](decisions.md) (G1–G11) |
| Survey | [survey.md](survey.md); your config: [config.md](config.md) |
| Tool contract changes | [api-changes.md](api-changes.md) |
| Evidence | Smoke test 2026-11-26 (revision 2), `~/smoke-out/2026-11-26/REPORT.md` (untracked) |
| Base | `main` @ 14a7f46 (plugin 0.3.0, protocol 3) |
| Status | **Done** except the live run. Unit and headless suites pass, Mao fixtures included (Results). The live suite needs Krita restarted on this checkout's plugin, and `live2d.enabled` in your config ([config.md](config.md)): both are your steps |

## Scope

- **The work order:** K8 (lint rule `reference-unbracketed`), K9
  (`update_layers` name warnings by kind) and K10 (the render hint),
  with the regression checks kept.
- **Your addition:** a survey that sorts krita-mcp's domain logic into
  three categories:
  1. inherent: always on
  2. usual: opt-out
  3. CONVENTIONS.md only: opt-in, with knobs

  The code is modularised to match. Lint is split into a core pack and a
  `live2d` pack. A top-level `live2d` config section turns the
  conventions on and holds their knobs.
- **Out of scope:** the umamo findings in the same report (U11 and on),
  and the `config_path` alias, which the work order makes optional (G8).

## Tasks

Status: ☐ open · ◐ in progress · ☑ done.

| # | Task | Spec | Status |
|---|---|---|---|
| T1 | Survey: every lint rule, tool warning and error, sorted into the three categories | yours | ☑ survey.md, G1 |
| T2 | `kritamcp/lint/` package: `engine` (registry, `Pack`, sets that expand to enabled rules, `rules_off`, per-pack severity, rename suggester), `core`, `live2d` | yours | ☑ G2, G4 |
| T3 | Config: `live2d` section (`enabled` + knobs), `lint.disable`, `lint.reference_pattern`, legacy `lint.additive_mode`; `/health` reports `live2d` | yours | ☑ G3, G5 |
| T4 | `set_canvas_size`'s `rig_breaking` only with `live2d.enabled`; the server's summary follows the warning | yours | ☑ |
| T5 | `reference-unbracketed`: hidden non-bracketed non-pose groups and top-level paint layers, both readings in the message, `suggested_names`, in `all` | K8 | ☑ G6 |
| T6 | Name-clash warnings within a kind, `name_shared_across_kinds` notes across kinds: `update_layers`, `duplicate_layer`, `merge_layers`, `selection_to_layer` | K9 | ☑ G7 |
| T7 | `render` hint dropped at `png_level` ≤ 1; `get_canvas`'s hint names `krita_render` | K10 | ☑ G8 |
| T8 | Docstrings (`krita_lint`, `krita_health`, `krita_set_canvas_size`, the four naming tools), README, architecture, plugin 0.4.0 | all | ☑ G9 |
| T9 | Tests: unit (packs, knobs, K8 cases, config, server summaries), headless (pack on/off, K9 notes, canvas), Mao (K8 acceptance on the `.ora` rebuild and the `.kra`), smoke replay (K8, K9, sizes); live tests read `live2d` from `/health` | all | ☑ G10 |
| T10 | Keep the smoke replay's `lint(all)` under 8 KB | regression | ☑ G11 |

## Results

Measured 2026-11-26 on `mao_pro_layered.kra` (the smoke test's fixture,
the same SHA-1) and its `.ora`, with `live2d.enabled`:

| Work order acceptance | Result | Test |
|---|---|---|
| K8: exactly one `reference-unbracketed` warning, for `Expression References`; `[ Guide Image ]` not flagged | 1 finding, `suggested_names` `[ Expression References ]` | `test_smoke_sizes` (`.kra`), `test_mao_headless.MaoLintTest` (`.ora` rebuild), live `test_lint_mao` |
| K8: `Arm L B` / `Arm R B` hidden (Stage 5.1) aren't flagged | Still exactly 1, and `pose-visibility` / `reference-hidden` 0 | `test_smoke_sizes` |
| K8: in `rules: ["all"]` | Yes (set `conventions`) | `test_lint`, `test_smoke_sizes` |
| K9: applying lint's 22 `suggested_names` gives no `duplicate_name` warning | `warnings: []`; `Wand A` / `Wand B` come back as `name_shared_across_kinds` notes | `test_smoke_sizes`, `test_update_move_delete` |
| K10: no `png_level=1` hint once it's in use | Dropped at levels 0 and 1, kept at 2–9 | `test_server_tools` |
| Regression: exactly 11 `duplicate-name` pairs with `suggested_names` | 11, all A/B, errors | `test_smoke_sizes`, `MaoLintTest`, live `test_lint_mao` |
| Regression: one-call lossless rebuild | `document_changes` {}, `projection_changed` false | `test_smoke_sizes`, `test_pixel_roundtrip` |
| Regression: `forbidden_path` enough to fix the config | Unchanged | `test_paths`, `test_smoke_sizes` |
| Regression: results ≤ 8 KB | Largest `lint(all)` 7.35 KB (7.97 KB before G11), `list_layers` 6.2 KB, `update_layers` (22 renames, with notes) 3.1 KB | `test_smoke_sizes` |

Suites: `make test` 1374 passed. `make test-krita` with
`KRITA_MCP_MAO_ORA` and `KRITA_MCP_MAO_KRA` set: 356 run, 0 failures,
0 skipped (5 skipped without them). On the `.ora` rebuild, lint `default`
gives `duplicate-name` 11 and `stray-alpha` 33. Lint `conventions` gives
`pose-visibility` 2 (the B poses are visible in the source) and
`reference-unbracketed` 1. kritarunner still aborts at exit after the
result marker (F003's known issue, exit code 134).

## Test plan

- `make test`: unit tests.
  - `tests/unit/test_lint.py`: `snap()` turns the Live2D pack on, and
    `generic()` keeps it off.
  - `test_config.py`: the `live2d` section, `disable`,
    `reference_pattern`, and the legacy key.
- `make test-krita`: the headless suite runs on the default config
  (conventions off). Tests of the conventions use `temp_config`. The Mao
  tests need `KRITA_MCP_MAO_ORA` and `KRITA_MCP_MAO_KRA`.
- `make test-live`: you need to do two things first:
  - restart Krita on this checkout's plugin (0.4.0)
  - add `"live2d": {"enabled": true}` to `~/.krita-mcp-config.json`
    ([config.md](config.md))

  Without the second, the two lint acceptance tests fail and say which
  key to set. The canvas test adapts either way.

## Risks

- **Agents that relied on the old defaults.** Without the config change,
  `krita_lint`'s `default` set is the 4 core rules, and `duplicate-name`
  is a warning. The summary says how many Live2D rules were left out and
  how to turn them on. `rules_off` lists them. `krita_health` shows
  `live2d.enabled`. The live acceptance tests fail loudly rather than
  quietly linting less.
- **`reference-unbracketed` on conforming art.** Hidden rig parts outside
  pose groups get a warning: closed eyes, toggled accessories, effect
  sprites that start hidden. That is the work order's intent ("why only a
  warning"). The message says to ignore it for a rig part, and
  `lint.disable` takes it out of the sets.
- **Custom patterns.** A `pose_pattern` other than the default diverges
  from umamo U11, which uses the default definition. config.md says so.
