# F001 acceptance procedures

How each acceptance test in [spec.md](spec.md#acceptance-tests) is set
up, run and judged. Every test is automated in
`tests/live/test_acceptance.py`, with the test function named below. Run:

```bash
KRITA_MCP_LIVE=1 KRITA_MCP_MAO_ORA=~/krita-mcp-output/mao_pro_layered.ora \
  .venv/bin/pytest tests/live/test_acceptance.py -v
```

Krita must be running with the plugin from this checkout. Tests work in a
scratch root, `~/krita-mcp-output/f001-acceptance/<run-id>/`, plus, for
test 5, a temporary "repo" root that the test adds to the config file and
removes afterwards.

## Fixtures

### Mao source (tests 1, 2)

The first session's workaround file, `~/krita-mcp-output/mao_pro_layered.ora`,
*is* the spec's "184 PNGs and the manifest". Its `stack.xml` holds the
structure, and `data/layer*.png` the pixels. Measured on 2026-11-24:

| Property | Value |
|---|---|
| Canvas | 5800 × 8400 |
| Paint layers | 184 |
| Groups | 38 (max depth 3) |
| Multiply layers | 7 (all others `svg:src-over`) |
| Hidden groups | 2: `Expression References`, `[ Guide Image ]` |
| Duplicate names | 17 names used twice (mostly a group and a layer with the same name, e.g. `Face`) |

`tools/ora_to_manifest.py <ora> <out_dir>` (P2.6) extracts the PNGs and
writes `manifest.json` (v1). The test runs it into the scratch root.

The duplicate names have two consequences:

- Name checks compare the **multiset of name paths**, not a set of names.
- The manifest links parents with `$key` refs. Bare-name refs would be
  `ambiguous`.

It also means lint on Mao *should* report 17 `duplicate-name` findings.
That makes a good secondary check, but it is not part of test 4.

The Mao art is a third-party sample. Never commit it or anything derived
from it to this repo. Tests that need it skip when `KRITA_MCP_MAO_ORA` isn't set.

### Lint fixture (test 4)

`tests/fixtures/lint_four_findings.kra`, built by
`tests/fixtures/build_lint_fixture.py`, a Scripter script, because filter
layers can't be created through our API. The file is small and committed.

*2026-11-25 (P4.3):* filter layers can be made headless
(`doc.createFilterLayer` with a standalone `Selection`, krita-api-notes
§ Nodes), so the builder is a module with `build(path)` that kritarunner
runs: `make lint-fixture` rebuilds the committed file (65 KB) through the
test shim's `build_lint_fixture` entry point. A rebuild differs only in
layer UUIDs and timestamps; tests read the committed file and never
rebuild it (the headless suite also builds one into a temp dir and lints
it). The default `Background` layer is removed; the filter is
`gaussian blur` over the whole canvas, and its blend mode is `copy`
(Krita's default for filter layers, which `blend-mode` doesn't check).

64×64, RGBA / U8 / sRGB. Stack, top to bottom:

```
Blur            filterlayer (Gaussian blur)        → node-type
Head            group
  Hair          paint, opaque 8×8 at (20, 4)
Back            group
  Hair          paint, opaque 8×8 at (36, 4)       → duplicate-name ("Hair", one finding)
Shade           paint, inherit_alpha, opaque 16×16 at (16, 24), inside Body → inherit-alpha
Body            paint, opaque 32×32 at (16, 24)
Spare           paint, no pixels                   → empty-layer
```

Every other default rule must stay quiet. So: hard-edged opaque
rectangles (no stray alpha), everything inside the canvas, Normal blend,
no layer styles, Title Case names with no side tokens, and an sRGB profile.

The committed snapshot `tests/unit/snapshots/lint_four_findings.json`
describes the same document, so the rules can be checked without Krita
(P4.2). The headless `tests/krita/test_lint_fixture.py` checks that it
equals the fixture's real snapshot, ids aside (P4.3).

## Tests

### 1. Mao rebuild without the workaround · `test_mao_rebuild`

1. `ora_to_manifest` into the scratch root.
2. `krita_new_canvas(5800, 8400, background="transparent")` → `doc`.
3. **One** `krita_create_layers(doc, manifest=<path>)` call.
4. `krita_save(doc, "<scratch>/mao.kra")`.
5. `krita_list_layers(doc)`.

**Pass:** 184 `paintlayer` and 38 `grouplayer` nodes, besides the
`Background` layer from `new_canvas`, which the test deletes or excludes.
The multiset of name paths equals the ORA's. The 7 Multiply layers are the
ORA's 7. Exactly the 2 groups above are hidden. Every layer's `bounds`
equals its PNG's offset and size, compared with `opaque_bounds` when the
PNG has transparent margins. The saved `.kra` reopens with the same
counts.

*P2.4 (2026-11-25):* the live test computes each PNG's opaque bounds with
Pillow (`getchannel("A").getbbox()`, offset by `x`, `y`) and excludes
`Background` by the id `new_canvas` returned. Its Mao-derived files go in
`<scratch>/mao`, removed after the test even with `KRITA_MCP_KEEP_SCRATCH=1`.
The same procedure runs headless in `tests/krita/test_mao_headless.py`
(`KRITA_MCP_MAO_ORA=… make test-krita KRITA_TESTS=mao`; kritarunner
inherits the shell's environment), in a temp dir deleted in `finally`.
First headless run: passed, `create_layers` 8.0 s, whole test 15.6 s
(convert 0.1 s, bounds check 2.4 s, save 3.7 s / 103 MB, reopen 1.2 s).

### 2. Round trip · `test_round_trip`, `test_round_trip_mao`

1. Open the `.kra` from test 1, or, in the small variant, a synthetic
   document with nested groups, Multiply/Add layers, a hidden group, an
   empty layer, partial alpha and an off-origin layer.
2. `krita_export_layers(doc, "all", dir=<scratch>/export)`.
3. `krita_new_canvas(w, h, background="transparent")` → `doc2`, then
   `krita_create_layers(doc2, manifest=<scratch>/export/manifest.json)`.
4. `krita_render(doc, a.png)` and `krita_render(doc2, b.png)`.

**Pass:** `a.png` and `b.png` decode, with Pillow in the test venv, to
identical RGBA arrays, **counting all fully transparent pixels as equal**
(colour under alpha 0 is not compared). `list_layers` of both match on
name paths, types, visibility, opacity and blend modes. The small variant
runs in the normal live suite, and must also be byte-identical including
colour under zero alpha. The Mao variant needs `KRITA_MCP_MAO_ORA`.

*Why zero-alpha colour is excluded (decided 2026-11-25, P3.2):* some Mao
source PNGs carry colour in fully transparent margins outside the layer's
alpha bounds. Krita's `bounds()` ignores them, `export_layers(crop=true)`
crops to alpha > 0, so they are dropped: the headless Mao round trip
differed in 11 pixels, all with alpha 0 in both documents. They are
invisible, and the rig renders premultiplied, so they can't show. This is
the mitigation the plan's risk table named. `crop=false` round-trips them
byte-identically if they ever matter.

*P3.4 (2026-11-25):* the renders come from `krita_render` (scale 1: the
projection's bytes exactly). Live, `test_round_trip` builds the small
document through the tools (`new_canvas` transparent, one `create_layers`
call with PNGs it writes with Pillow into `<scratch>/round-trip`, each with
opaque corners so the crop keeps every byte, colour under alpha 0 inside);
`test_round_trip_mao` rebuilds Mao as test 1 does (shared helper), then
exports at `png_level=1`, rebuilds and renders at `png_level=1`, all in
`<scratch>/mao`. Headless, the same procedure runs in
`tests/krita/test_export_layers.py` (`RoundTripTest`: byte-identical) and
`tests/krita/test_mao_headless.py` (renders identical but for the 11
alpha-0 pixels above). Not yet run live.

### 3. Canvas cap · `test_canvas_cap`

- `krita_new_canvas(5800, 8400)` succeeds and `get_document_info`
  reports 5800 × 8400, RGBA / U8 / sRGB.
- `krita_new_canvas(16385, 16)` fails with `too_large` (per-side limit).
- `krita_new_canvas(9000, 9000)` fails with `too_large` (81 MP is over 67.1 MP).
- A config file override of `max_canvas_pixels` is honoured, then restored.

### 4. Lint · `test_lint_fixture`

`krita_open_file(<fixture>)`, then `krita_lint(doc)`.

**Pass:** the findings are exactly these, as a multiset of
`(rule, layer path)`:

```
(node-type,      "Blur")
(inherit-alpha,  "Shade")
(duplicate-name, "Back/Hair")     # related_ids has both Hair layers
(empty-layer,    "Spare")
```

Written as `tests/live/test_acceptance.py::test_lint_fixture` (P4.4),
which opens a copy of the committed fixture from the scratch dir.
`test_lint_mao` (with `KRITA_MCP_MAO_ORA`) is the Mao secondary check:
exactly 17 `duplicate-name` findings once `new_canvas`'s `Background` is
deleted; headless it also gives 48 `stray-alpha` findings and nothing else
(2026-11-25).

### 5. Allowlist · `test_allowlist`

Setup: make a temp dir `R` (standing in for a project repo), add it to
`~/.krita-mcp-config.json` under `allowed_dirs`, and make a symlink
`R/escape → /tmp`.

| Call | Expected |
|---|---|
| `krita_save(doc, "R/art/x.kra")` (absolute) | OK; returns the resolved absolute path; file exists |
| `krita_save(doc, "x.png")` | OK; written to the output dir |
| `krita_save(doc, "/tmp/x.png")` | `forbidden_path` |
| `krita_save(doc, "R/../x.png")` | `forbidden_path` |
| `krita_save(doc, "R/escape/x.png")` | `forbidden_path` (symlink resolved) |
| `krita_save(doc, "R/x.txt")` | `forbidden_path` (extension) |
| `krita_export_layers(doc, dir="R/escape/out")` | `forbidden_path` |
| Config file made world-writable | Roots from the file ignored; `krita_health` reports the error |

Teardown restores the config file.

*Phase 1 (P1.7):* every row but `krita_export_layers` runs in
`test_allowlist`; that row lands with P3.2 (`test_allowlist_export_layers`
skips until then). `R` is `<system temp>/kmcp-f001-*/R`, outside the
output dir and every root, parent included; the relative `x.png` goes
into the run's scratch dir.

*P3.2 (2026-11-25):* the `krita_export_layers` row is
`test_allowlist_export_layers` (written, not yet run live): it checks that
`/tmp/out` is outside every root, expects `forbidden_path` with nothing
created there, then exports into `R/out` (resolved path, manifest written)
and gets `conflict` for the now non-empty directory.

### 6. Handles · `test_handles`

1. `krita_new_canvas(…, name="A")` → `a`; `krita_new_canvas(…, name="B")` → `b`.
   The last one created is active in the UI.
2. `krita_create_layers(doc=a, specs=[{name: "Only In A"}])`.
3. `krita_list_layers(doc=a)` has it, and `krita_list_layers(doc=b)` doesn't.
4. `krita_list_documents()` shows `b` active, `a` not.
5. Painting with `doc=a, layer="Only In A"` changes `a`'s pixels, checked
   with `get_color_at(doc=a, layer=…)`, and leaves `b` unchanged.
6. A tool call with no `doc` hits `b`.
7. `krita_close_document(a, discard=true)` (P1). If P1 isn't there yet,
   close through Krita. Then a call with `doc=a` fails with `not_found`.

*Phase 1 (P1.7):* steps 2, 3 and 5 painted into each canvas's
`Background` layer instead. *P2.4 (2026-11-25):* `test_handles` now has the
full form above; step 3 also checks that `get_color_at(doc=b, layer="Only
In A")` is `not_found`.
