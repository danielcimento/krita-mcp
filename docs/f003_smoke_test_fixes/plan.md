# F003 plan: fixes after the first smoke test

| | |
|---|---|
| Request | [spec.md](spec.md) (work order "Spec v2", K1–K7) |
| Decisions | [decisions.md](decisions.md) (E1–E9) |
| Tool contract changes | [api-changes.md](api-changes.md) (deltas against [../f001_buildout/api-design.md](../f001_buildout/api-design.md)) |
| Evidence | Smoke test 2026-11-25, `live2d-workspace` branch `smoke-test/2026-11-25`, `smoke-out/REPORT.md` (untracked) |
| Base | `local-fixes` @ a6549d4 (plugin 0.2.0, protocol 2) |
| Status | **Done.** Unit, headless (Mao fixtures included) and live suites pass (see Results); live run 2026-11-26 with `KRITA_MCP_MAO_ORA`, 25/25 after updating `test_lint_mao` from F001's 17 duplicate names to K6's 11 |

## Scope

The work order's K1–K7 against the F001 tool set, keeping every F001
behaviour the smoke test rated well (its "Regression checks" list). No new
painting features, nothing for umamo (its findings are in the same report
but belong to the umamo spec).

## What the smoke test actually measured (K1)

The work order guessed that the `export_layers` → `create_layers` round
trip loses precision in a premultiplied `QImage` path. Reproducing step
3.3 headless on `mao_pro_layered.kra` showed otherwise (decisions.md E1):

| Comparison | Pixels with alpha > 0 that differ | Alpha-0 pixels whose RGB differs |
|---|---|---|
| Every paint layer, `pixelData()` source vs rebuild | **0** (all 184 layers byte-identical; all properties equal) | 0 |
| Composite (`Document.pixelData`), source vs rebuild | 316, max ±1 | 2,870 |
| Rebuild vs a rebuild of the rebuild's export | 0 | 0 |
| Composite after giving each rebuilt layer the source's paint-device origin (`Node.move`) | **0** | 11 |

So the PNG path was already byte-exact. The difference is in how Krita
composites: all 184 layers of the source `.kra` have a non-zero
paint-device origin (`Node.position()`, e.g. `(1603, 4423)` for Hand R),
which shifts their 64 px tile grid; a layer made by `createNode` has its
origin at (0, 0). Krita's compositing rounds differently depending on
where the tile boundaries fall, by ±1 at low alpha. Reproducing the origin
makes the composite match. The remaining 11 alpha-0 pixels are colour
under alpha 0 outside each layer's alpha bounds, which the export crop
dropped (E2).

## Tasks

Status: ☐ open · ◐ in progress · ☑ done.

| # | Task | Spec | Status |
|---|---|---|---|
| T1 | Diagnose the 3.3 difference (headless repro on the Mao `.kra`) | K1 | ☑ E1 |
| T2 | `export_layers` records each paint layer's `device_origin`; `create_layers` (specs and manifest) applies it before writing pixels | K1 | ☑ |
| T3 | Export crop keeps alpha-0 colour: read the layer's tile-aligned window, crop to its alpha > 0 pixels plus alpha-0 pixels that carry colour (E2) | K1 | ☑ |
| T4 | Pixel-path tests: 0–255 alpha ramp over saturated colours survives 10 `export_layers` → `create_layers` round trips and 10 `import_png` → export round trips; Mao `.kra` round trip is byte-identical per layer and for the composite | K1 | ☑ |
| T5 | `forbidden_path` errors name the allowed folders, the config file, the key and the fix; `/health` adds `config_key`, `config_exists`, `config_reload`; new `krita_reload_config` | K2 | ☑ |
| T6 | `new_canvas(background="none")`; `create_layers(manifest, new_document=true)` makes the document from the manifest's `document` block, returns its handle | K5 | ☑ |
| T7 | `duplicate-name` checks groups and layers in separate namespaces; messages give group paths and a suggested fix (pose suffix when the copies sit in pose groups) | K6 | ☑ |
| T8 | `stray-alpha` findings carry the speck pixel count and bounds | K7 | ☑ |
| T9 | `draw_shape`, `stroke` and `fill` take an optional `color` | K7 | ☑ |
| T10 | `diff_documents` quantifies pixel changes (count, bounds, max channel difference, alpha-0-only count) for the composite and each layer; `ignore_alpha0_rgb` | K4 | ☑ |
| T11 | Bulk results: `list_layers` (`fields` default, `summary_only`, `path`, `limit`/`offset`), `export_layers` (`verbose`), `lint` (`group_by`, `limit`, `offset`), `create_layers` (`verbose`) | K3 | ☑ |
| T12 | Size check: replay the smoke test's calls with the tools' defaults on the Mao `.kra`, every result ≤ 8 KB | K3 | ☑ |
| T13 | Protocol 3 / plugin 0.3.0; README, architecture, krita-api-notes; regression run (unit, headless, Mao, live) | all | ☑ |

## Results

Measured headless on `mao_pro_layered.kra` (5800×8400, 222 nodes),
2026-11-26:

| Work order acceptance | Result | Test |
|---|---|---|
| K1.1 every layer's `pixelData()` identical after `export_layers(all)` → `create_layers(manifest)` | 0 differing layers (184), device origins equal | `test_pixel_roundtrip.MaoKraRoundTripTest` |
| K1.2 composite identical for alpha > 0 | Identical byte for byte, alpha-0 colour included | same |
| K1.3 alpha ramp, 10 round trips | Unchanged (export→create and import_png→export, 10 each) | `test_pixel_roundtrip.SyntheticRoundTripTest` |
| K2 agent can fix the config from the error alone | The message gives the folder, file, key and JSON; a unit test writes that JSON and the path is then allowed | `test_paths.test_forbidden_fix_works_when_applied`, `test_f003_tools.ConfigTest` |
| K3 every smoke-test call ≤ 8 KB by default | Largest: `lint(rules=all)` 7.1 KB, `list_layers` 6.2 KB, `update_layers` (22 renames) 2.9 KB; the rest under 1.2 KB (was 66 KB / 72 KB) | `test_smoke_sizes` |
| K4 the 3.3 situation is quantified | 316 visible pixels, max difference 1, plus 2,859 alpha-0 pixels, each with bounds (the smoke test's 2,870 included 11 that E2 now keeps) | `MaoKraRoundTripTest._without_origins` |
| K5 rebuild in one call, no document-level differences | `create_layers(manifest, new_document=true)`: `document_changes` {} | `test_smoke_sizes`, `MaoKraRoundTripTest` |
| K6 exactly 11 duplicate-name errors, all A/B pairs | 11, each with `suggested_names`; applying them in one `update_layers` call clears the rule | `test_smoke_sizes`, `test_mao_headless.MaoLintTest`, live `test_acceptance.test_lint_mao` |
| K7 stray-alpha pixel count and bounds; colour on draw calls | Present on every finding; `draw_shape` / `stroke` / `fill` take `color` | `test_smoke_sizes`, `test_f003_tools` |

Regression checks from the work order: covered by the existing suites,
which still pass (create_layers nesting, batches as one undo step,
`selection_to_layer(cut)` + `undo`, occlusion, duplicate + flip, layer
stats, `new_canvas(5800, 8400)`, allowlist refusals with `..` and
symlinks).

## Known issue

With `KRITA_MCP_MAO_KRA` set, `kritarunner` aborts **at exit**, after
every test has passed and the result marker is written (exit code 134,
`QPixmap: Must construct a QGuiApplication before a QPixmap`). It comes
from `test_smoke_sizes` and needs the Mao document saved a second time in
the same process after an edit. The same steps on a 64×64 document exit
cleanly. `make test-krita` judges by the marker line, so results are
unaffected, and a QPixmap without a QGuiApplication can't happen in the
GUI. Whether F001 code hit it before is unknown: no F001 test saved the
Mao document twice. Not investigated further.

## Test plan

- `make test`: unit tests (pure modules, server wrappers).
- `make test-krita`: headless suite. F003's own: `test_pixel_roundtrip`,
  `test_smoke_sizes`, `test_f003_tools`. The Mao tests need
  `KRITA_MCP_MAO_ORA` (existing) and, new here, `KRITA_MCP_MAO_KRA`
  (the layered `.kra`, `~/krita-mcp-output/mao_pro_layered.kra`).
- `make test-live`: needs Krita restarted on this checkout's plugin
  (protocol 3), which is the user's step.

## Risks

- **`Node.move` on an empty layer** sets the paint device's origin without
  moving pixels (checked headless, E1). If a later Krita changes that, T2's
  test catches it: the rebuilt layer's `position()` must equal the source's.
- **Bigger PNGs from T3**: a layer whose alpha-0 margin carries colour now
  exports that margin. The manifest's `x, y, w, h` describe the PNG, so
  consumers that took the PNG rect as the alpha bounds must use `bounds`
  from `list_layers` instead (api-changes.md).
- **Tool defaults change shape** (T11): agents and tests that relied on
  the full `list_layers` / `export_layers` / `create_layers` / `lint`
  output must pass `verbose`, `fields` or `limit`. The plugin commands keep
  their old defaults (E8), so only the MCP tools change.
