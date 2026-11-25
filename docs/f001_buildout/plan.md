# F001 buildout plan: Live2D art-preparation extensions

| | |
|---|---|
| Request | [spec.md](spec.md) |
| Tool contracts | [api-design.md](api-design.md) |
| Decisions | [decisions.md](decisions.md) |
| API spike | [spike.md](spike.md) |
| Acceptance procedures | [acceptance.md](acceptance.md) |
| Status | Phase 1 done (live acceptance 3, 5 and 6 passed 2026-11-25; 5 minus the export row, 6 in its Phase-1 form). Phase 2 next. Hand-off: [handoff-prompt.md](handoff-prompt.md) |

## Scope

Add document handles, a layer-tree API, file-based pixel I/O, rendering,
and Live2D linting to krita-mcp, so an agent can build, restructure and
check rig-ready `.kra` files without the OpenRaster workaround. The work is
ordered so that all **P0** tools and acceptance tests 1–6 are done before
any P1 work starts. P2 comes last.

Out of scope, as in the spec: generative painting inside Krita, and
animation. New painting features are also out (D15).

## What the current code means for the plan

Issues found reviewing `server.py` and `krita-plugin/kritamcp/__init__.py`
at `7483197`. Each one is fixed in the phase shown.

| # | Finding | Fix in |
|---|---|---|
| 1 | `MAX_CANVAS_DIM = 8192` per side. Mao is 5800×**8400**, so the spec's example cap (8192×8192) would still refuse it | P1.4 (D8) |
| 2 | `new_canvas` / `clear` build `bytes([b, g, r, 255] * n)`, a Python list of 4·n ints first (about 1.6 GB at 48.7 MP) | P1.7 (fill fixed early, in P1.4, when the caps went up) |
| 3 | When `get_result` times out, the command still runs later, and its result stays in `CommandQueue.results` forever | P1.5 (D9) |
| 4 | Single-threaded `HTTPServer`: `/health` waits behind a long command | P1.5 |
| 5 | Validation errors go out as HTTP 500, and the server turns every error into a *successful* tool result reading "Error: …" | P1.3 (D2) |
| 6 | `krita_save` returns the requested path, not the resolved one | P1.7 |
| 7 | `get_color_at` checks coordinates against `MAX_CANVAS_DIM`, not the document size | P1.6 |
| 8 | The painting tools write with `setPixelData`, which creates no undo step (spike Q1). So `krita_undo` today undoes an unrelated earlier step instead | P5.1 (D6) |
| 13 | `new_canvas` adds a paint layer on top of the white `Background` that `createDocument` already makes (spike Q4) | P1.7 |
| 9 | `get_active_layer()` may return a group or mask. `setPixelData` then fails without an error | P1.6 |
| 10 | `open_file` uses `doc.name()`, which is empty for `.ora` | P1.7 |
| 11 | The server can't tell when the installed plugin is an older version | P1.3 (protocol version) |
| 12 | Minor: unused `threading.Event()` in `get_result`, `command_counter` not locked (needed once the server is threaded), `set_brush` wrapper drops `size=0` | P1.5 / P1.3 |

## Target layout

```
server.py                      # MCP entry point (path unchanged for client configs)
krita_mcp_server/              # server-side helpers, imported by server.py
  client.py                    #   call(action, params, timeout) -> dict | raises ToolError
  models.py                    #   pydantic models: LayerSpec, LayerChange, Move, Rect, …
krita-plugin/kritamcp.desktop
krita-plugin/kritamcp/
  __init__.py                  # registers the Extension; nothing else
  extension.py                 # KritaMCPExtension: timer, starts the bridge
  bridge.py                    # ThreadingHTTPServer, auth, CommandQueue, timeouts
  dispatch.py                  # @command registry: name, timeout, mutating flag
  errors.py                    # CommandError(code, message, details)
  config.py        (pure)      # config file + env var → roots, limits
  paths.py         (pure)      # resolve_allowed_path, resolve_output_dir
  params.py        (pure)      # int/float/bool/enum/rect validators
  refs.py          (pure*)     # layer-ref parsing and matching over a node index
  manifest.py      (pure)      # manifest v1 read/write/validate
  lint.py          (pure)      # rules over a snapshot
  registry.py                  # document handles
  nodes.py                     # Node → layer object, tree walk, node index
  pixels.py                    # BGRA ↔ QImage ↔ PNG, alpha stats, compositing
  batch.py                     # validate → apply → rollback; journal
  commands/
    documents.py  layers.py  pixels.py  render.py  selection.py
    analysis.py   paint.py   history.py
tests/
  unit/                        # pytest, no Krita
  live/                        # KRITA_MCP_LIVE=1, against a running Krita
  fixtures/                    # small committed fixtures + builder scripts
tools/
  ora_to_manifest.py           # first-session .ora → manifest v1 + PNGs (acceptance 1)
  install_plugin.sh            # symlink plugin into pykrita
requirements-dev.txt           # pytest, Pillow (PyQt5 code is tested headless, in Krita's Python)
```

`(pure)` modules don't import `krita` or need a running Krita, so they can be unit-tested in the
repo venv. `refs.py` is pure apart from a thin adapter, because it
matches against a plain index built by `nodes.py`.

Plugin action names, the response envelope and the MCP tool names are
listed in [api-design.md](api-design.md).

## Phases

Each bullet is about one commit. Every commit leaves the plugin
installable and the existing tools working.

### Phase 0: API spike

- [x] Write the probe script and the question list ([spike.md](spike.md)).
- [x] First run (2026-11-24): crashed at the Q14 `Node.save` call (see
      [spike.md § Run log](spike.md#run-log)). The probe was fixed to save
      results after every probe, and `Node.save` moved to Q16.
- [x] Run 2: all probes finished, then Krita crashed in the probe's
      cleanup because a closed document was touched again. Results are
      recorded in `spike.md`, `krita-api-notes.md` and the decisions. D6 is closed.
- [x] Run 3: clean. Every question that blocks work is answered. D3, D6
      and D10 are closed.
- [x] Q12: `kritarunner` works headless. D16 closed (headless suite
      calls dispatch directly).
- [x] Q11: timings at Mao size. Krita is fast and PNG encoding
      dominates. D9's defaults hold. D10 switched to clone-based rendering.

*Exit:* met. No open or provisional decisions remain.

Phase 1 can start.

### Phase 1: Foundation

Mostly internal. User-visible changes: structured results, configurable
roots, larger canvases, handles, and `list_documents` / `get_document_info`.

- [x] **P1.1 Test and dev scaffolding.** `tests/unit`, `tests/live` with a
  `KRITA_MCP_LIVE` skip marker, `requirements-dev.txt`, `pytest.ini`,
  `tools/install_plugin.sh` (symlink install), README dev section. Also
  the headless suite (D16): `tests/krita/` (unittest), the
  `kmcp_test_runner.py` shim, `tools/install_test_runner.sh`, and a
  `Makefile` with `test`, `test-krita` and `test-live`.
- [x] **P1.2 Split the plugin into modules, no behaviour change.** Move code
  into `bridge.py`, `extension.py`, `paths.py`, `params.py` and
  `commands/paint.py`, and keep the `if/elif` dispatch. Unit tests for
  `paths.resolve_allowed_path` and `params`, covering `..`, symlinks and
  extensions. These tests pin the current behaviour before it changes.
  Move token creation out of import time into extension startup, so the
  headless suite can import the package without side effects.
- [x] **P1.3 Dispatch registry, error envelope, protocol version.**
  `dispatch.py` + `errors.py`; handlers return payloads and raise
  `CommandError(code, …)`. The envelope and HTTP codes follow D2. `/health` reports
  `protocol: 2`. In `server.py`, `client.call()` checks the protocol once per process,
  raises `ToolError`, and all 15 existing tools return dicts with a
  `summary`. Unit tests for the server wrappers with the HTTP call mocked.
- [x] **P1.4 Config and limits.** `config.py` (D7, D8): config file, env
  var, mtime reload, permission check, root checks, limit overrides.
  `paths.py` uses the resolved roots, and gains `resolve_output_dir`
  (create, check emptiness) and the `.json` manifest exception.
  `/health` reports roots and config errors. Unit tests use temp dirs and
  symlinks.
- [x] **P1.5 Queue and timeouts.** `ThreadingHTTPServer`, locked counter,
  per-command default timeout plus `timeout_s`, abandoned-command
  handling, `elapsed_ms` measured on the main thread, `busy` in `/health`
  (D9). `server.py` derives its `httpx` timeout from the command's.
  Unit-test `CommandQueue` with a fake executor thread.
- [x] **P1.6 Handles and refs.** `registry.py` (D3), `nodes.py` (tree walk,
  index, layer object), `refs.py` (D4). Every existing tool gains `doc`,
  and painting tools gain `layer`. Painting into a non-paint layer fails
  with `conflict`. `get_color_at` uses document bounds. The plugin keeps
  the foreground color itself (D15). Unit tests for `refs` over a fake
  index: UUID with and without braces, paths, escaped `/`, ambiguity.
- [x] **P1.7 Document tools.** `list_documents`, `get_document_info`, the
  reworked `new_canvas` (limits, transparent background, profile, memory
  fix), `open_file` (handle, `already_open`, name fallback), and `save`
  (in place / `saveAs` for `.kra` / `exportImage`, resolved path, size).
  Live tests. *(Also `close_document`, pulled forward from P5.2. All file
  I/O goes through `kritamcp/fileio.py`, the D5 batch-mode helper. The
  live acceptance tests 3, 5 and 6 are in `tests/live/test_acceptance.py`,
  run by hand with `make test-live`; 6 is adapted until P2.3.)*

*Exit:* acceptance tests **3** (canvas cap), **5** (allowlist) and **6**
(handles) pass. README configuration section updated.

*Met 2026-11-25:* `make test-live` passed 4, skipped 1 (the `export_layers`
row of test 5, which lands with P3.2). Test 6 ran in its Phase-1 form
(painting `Background`), and gets its full form after P2.3.

### Phase 2: Layer tree (P0)

- **P2.1 `list_layers`.** Layer object serialization, `root`, `depth`,
  `fields`, counts. Bounds per Q2.
- **P2.2 Batch executor.** `batch.py`: validate-all, apply with recorded
  inverses, rollback on failure, `dry_run`. It also hands the inverse
  list to a per-document journal, which stays unused until P5.1. Unit
  tests with a fake applier: rollback order, partial failure report.
- **P2.3 `create_layers` (specs).** Groups and paint layers, `key` / `$key`
  refs, `above`, properties, and `png` + `x`/`y`, which go through
  `pixels.load_png_bgra` (the only pixel helper this phase needs).
- **P2.4 `manifest.py` + `create_layers(manifest=…)`.** Manifest v1 schema
  validation (unit-tested) and path resolution relative to the manifest.
- **P2.5 `update_layers`, `move_layers`, `delete_layers`.** Including
  the cycle check for moves and subtree reporting for deletes.
- **P2.6 `tools/ora_to_manifest.py`.** Converts the first-session
  `mao_pro_layered.ora` to manifest v1 plus PNGs. It runs outside Krita,
  using `zipfile` and `xml.etree`. Mapping: stack.xml is top to bottom, so
  reverse it; `svg:src-over`→`normal`, `svg:multiply`→`multiply`,
  `svg:plus`→`add`; `visibility`, `opacity`, `x`, `y`; `isolation`→`pass_through`.
  The Mao file uses `isolation="auto"` throughout. Map it to whatever
  Krita did when it imported the same file: compare with the group flags in
  `~/krita-mcp-output/mao_pro_layered.kra`, which Krita saved from this
  `.ora`. Unit-tested on a small synthetic `.ora`.

- [ ] **P2.7 Additive blend conversion on export (D17).** `save` to
  `.psd` converts `add` → `linear_dodge`, to `.ora` `linear_dodge` → `add`,
  on a `clone()`; result lists `blend_conversions`. Headless test reopens
  the exported file and checks the modes.

*Exit:* acceptance test **1** (Mao rebuild) passes.

### Phase 3: Pixels (P0)

- **P3.1 `pixels.py`.** BGRA↔QImage wrap and copy, PNG load/save,
  alpha extraction, opaque-bounds scan (row and column passes over the alpha
  bytes), source-over compositing with `QPainter`, and a guard for
  8-bit RGBA documents. Headless tests (`tests/krita`) cover byte order
  and bit-exact round trip (mirroring Q5).
- **P3.2 `export_layers`.** Per-layer PNGs, cropping to opaque bounds,
  hidden layers, empty layers recorded with `png: null`, optional group
  projections, and the manifest. Always the `pixelData` → QImage path,
  never `Node.save`, which bakes opacity in and can open a dialog (D5).
- **P3.3 `import_png`.** `replace` / `over` / `clear_outside`, paint-layer
  check, and the changed rectangle in the result.
- **P3.4 `render`.** Region, scale, `only` / `hide` resolution
  (ancestors shown, descendants keep their state), background, and
  `png_level`. Subset renders go through a `clone()` closed in `finally`
  (D10 B). A headless test checks that the clone's projection follows
  visibility changes in the clone.
  `get_canvas` becomes a shortcut for `render`.

*Exit:* acceptance test **2** (round trip, pixel-identical) passes. It
also runs on a small synthetic document in the normal live suite, so it
doesn't need Mao.

### Phase 4: Lint (P0)

- **P4.1 Snapshot collector.** In `commands/analysis.py`: tree, properties,
  `has_layer_style`, document color info, and per-paint-layer alpha
  stats (`pixel_count`, `opaque_bounds`, the stray-alpha count from
  `Selection.grow` per Q10, with a tile-based fallback).
- **P4.2 `lint.py` default rules.** The ten rules in
  [api-design.md § Lint rules](api-design.md#lint-rules), each a pure
  function with a hand-written-snapshot unit test, including the
  acceptance-test-4 scenario as a snapshot.
- **P4.3 Lint fixture.** `tests/fixtures/build_lint_fixture.py`, a
  Scripter script, since filter layers can't be made through the API, and
  the committed `tests/fixtures/lint_four_findings.kra` it produces. See
  [acceptance.md § Fixtures](acceptance.md#fixtures).
- **P4.4 `krita_lint` tool.** Rule selection, `severity_min`, sorting
  by stacking order.

*Exit:* acceptance test **4** passes. **All P0 done.** Update the README
tool table and docs, and run the full acceptance suite once on Mao.

### Phase 5: P1

The order puts the part-separation workflow first, then convenience
tools.

- **P5.1 Agent journal undo (D6).** `krita_undo` / `krita_redo` work on
  the journal, and the old triggers move to `krita_ui_undo` / `krita_ui_redo`.
  Pixel snapshots for mutating pixel tools: `import_png` from P3 and
  everything below. Memory and disk caps. Live test: create, update, move,
  delete, import, then undo × 5 restores the document.
- **P5.2 `set_active_layer`, `duplicate_layer`, `close_document`.**
  `close_document` done early in P1.7.
- **P5.3 `layer_stats`, `trim_layers`.** These reuse the P4.1 stats code.
- **P5.4 `select`.** rect / polygon (rasterized with `QPainter`) /
  alpha_of / invert / none / all, combined with replace / add / subtract /
  intersect, plus grow / shrink / feather.
- **P5.5 `selection_to_layer`.** Our own masked copy, unless Q9 shows
  `Selection.cut` / `paste` do the right thing.
- **P5.6 `transform_layer`.** Translate, and an exact flip with
  `QImage.mirrored`. Scale and rotate use `scaleNode` / `rotateNode` if Q6
  shows usable pivots, otherwise `QTransform` on the pixels.
- **P5.7 `merge_layers`.** Group flatten through the projection, and
  multi-layer merge through a temporary group. Opacity handling per Q8.
- **P5.8 `occlusion_map`.** Builds on `render(only=…)` and the alpha helpers.
- **P5.9 Lint `conventions` set.** The six extra rules.

### Phase 6: P2

- **P6.1 `set_canvas_size`**, with the rig-breaking warning.
- **P6.2 `diff_documents`.** UUID, then path, then name matching,
  property deltas, and pixel deltas over the union of bounds.

## Definition of done for each tool

- [ ] Plugin handler registered with its timeout and mutating flag, and
      all inputs validated with the shared `params` helpers.
- [ ] `server.py` wrapper with typed parameters (pydantic models for
      list items, `extra="forbid"` so typos fail), and a docstring that states
      units and defaults.
- [ ] Unit tests for every pure part. A live test in `tests/live/`.
- [ ] Mutating tools: `dry_run`, rollback on failure, journal entry.
- [ ] `api-design.md` matches what was built. README tool table updated.

## Testing

Per D16. Three suites:

| Suite | Command | Needs |
|---|---|---|
| Unit | `make test` (`.venv/bin/pytest tests/unit`) | Nothing |
| Headless Krita | `make test-krita` | `tools/install_test_runner.sh` once. The GUI Krita may stay open |
| Live | `make test-live` | GUI Krita running with the plugin from this checkout |

Headless tests cover the command handlers through dispatch. Live tests
cover the HTTP bridge, `server.py` and the acceptance tests. For live
tests, start Krita with the plugin installed from this checkout (by
symlink, see `tools/install_plugin.sh`), then run
`KRITA_MCP_LIVE=1 .venv/bin/pytest tests/live`. Live tests create a
scratch root under the output dir, add it to the config file for the
session, and remove it at the end. They never touch documents they didn't
open. Mao-based tests also need `KRITA_MCP_MAO_ORA` set and are skipped
otherwise.

The Mao sample is third-party art. Its PNGs and derived files are never
committed to this repo. Tests read them from the user's machine.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| libkis edits aren't undoable (Q1) | The spec's "one undo per action" can't use Krita's undo | Agent journal (D6). Document how it interacts with UI edits |
| `Node.bounds()` is tile-aligned (Q2) | Crops and lint would be wrong | Compute `opaque_bounds` from alpha (P3.1), and use Krita's bounds only as the scan window |
| PNG round trip not bit-exact (Q5), e.g. zero-alpha colors | Acceptance test 2 fails | Compare renders, which are composites, not layer bytes. If needed, normalize zero-alpha pixels on export and document it |
| Large docs are slow on the main thread | Timeouts, frozen UI | Q11: Krita's own operations take milliseconds at Mao size; file I/O takes 1–3 s per file. Per-command timeouts up to 900 s, batch cap, `busy` in health, `png_level` and `scale` for speed. Note in the docs that Krita is unresponsive during big exports |
| Rendering by toggling visibility leaves state behind after a crash mid-render | Visibility changed in the user's doc | `finally` restore. Fall back to clone rendering (D10 B) |
| A dialog blocks the main thread during save/export/close. **Seen:** `Node.save` opened the PNG export dialog, and dismissing it crashed Krita | The command hangs; Krita may crash | Never call `Node.save`. Run every export through one batch-mode helper (D5). `setModified(False)` only when the caller passes `discard`. Probe results Q13 and Q16 decide the rest |
| The plugin and server get out of step after an update | Confusing errors | Protocol version check (P1.3) |
| Pure-Python pixel loops creep in | Minutes per call on 48 MP layers | Rule: no per-pixel Python loops over whole layers. Use `bytes` slicing/`translate` or `QPainter`, and review for it |

## Effort

Rough sizes, for sequencing rather than commitments. S ≈ half a day,
M ≈ 1–2 days, L ≈ 3+ days.

| Phase | Size |
|---|---|
| 0 Spike (after the probe run) | S |
| 1 Foundation | L |
| 2 Layer tree | M–L |
| 3 Pixels | M |
| 4 Lint | M |
| 5 P1 | L |
| 6 P2 | M |
