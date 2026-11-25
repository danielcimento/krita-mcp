# F001 API spike (Phase 0)

Several design choices depend on how Krita 5.3.4's Python API behaves, and
the type stub doesn't settle that. This phase answers those questions
before code depends on them.

## How to run

1. In Krita: **Tools → Scripts → Scripter**, then open
   [`spike/krita_api_probe.py`](spike/krita_api_probe.py) and **Run**.
2. Results print in Scripter and go to
   `~/krita-mcp-output/spike/probe-results.json`.
3. Optionally set `RUN_LARGE = True` and run again for Q11. That needs
   about 2 GB of RAM and a minute or two.
4. Do Q12 by hand (below).
5. Fill in the *Result* column here, update
   [../krita-api-notes.md](../krita-api-notes.md) (change *[to verify]* to
   *[verified]* or correct the entry), and close the matching decisions in
   [decisions.md](decisions.md). Commit the JSON as
   `spike/probe-results.json`.

The script creates and closes its own `probe-*` documents and doesn't
touch open work. Each probe runs in its own `try`. A bug in one probe
records `"status": "error"` with a traceback and the run continues, so
fix the probe and rerun rather than reading anything into that result.

## Questions

| # | Question | Decides | Result |
|---|---|---|---|
| Q0 | Krita and Python versions, numpy, byte order, scaling filter names | D5, `transform_layer` `resample` values | |
| Q1 | Which libkis calls create Krita undo steps (`addChildNode`, `setName`, `setVisible`, `setOpacity`, `setBlendingMode`, `setInheritAlpha`, `setPixelData`, `move`, `remove`, reorder, `setActiveNode`)? | D6 (how the journal is documented, and what `krita_ui_undo` means), D10 | |
| Q2 | Is `Node.bounds()` exact or tile-aligned? Does it shrink after pixels are cleared? Empty layer bounds? Off-canvas pixels? | `list_layers.bounds`, export cropping, lint `empty-layer` / `out-of-canvas`. If not exact, compute `opaque_bounds` from alpha | |
| Q3 | UUID string format; `nodeByUniqueID` with and without braces; does `clone()` keep IDs; do `.kra` / `.ora` keep IDs? | D4, D10 option B (mapping nodes into a clone), `diff_documents` | |
| Q4 | Does `createDocument` add a default layer? Default profile name; available RGBA/U8 profiles; `Document ==` in `documents()` | `new_canvas`, D3 | |
| Q5 | BGRA order confirmed; `setPixelData`→`pixelData` and QImage→PNG→QImage bit-exact, including partial and zero alpha | D5; acceptance test 2 (pixel-identical round trip) | |
| Q6 | `move(x, y)` absolute or relative; `scaleNode` / `rotateNode` origin and pivot | `transform_layer` | |
| Q7 | Does `setVisible` set `modified`? Can it be reset? Does the projection follow after `refreshProjection` + `waitForDone`? Clone cost | D10 (render A vs B) | |
| Q8 | Does a group's `projectionPixelData` include the group's own opacity? Does it bake inherit-alpha clipping? | `merge_layers` | |
| Q9 | Do `Selection.cut` / `paste` write into the given layer, or create a floating layer? Does `Document.setSelection(None)` deselect? | `selection_to_layer` (use the API or do it ourselves), `select(none)` | |
| Q10 | Does `Selection.grow` dilate (square or disc kernel)? Does `setPixelData` need a prior `resize`? | Lint `stray-alpha`, `trim_layers(near_px)` | |
| Q11 | At 5800×8400: time for full-layer `setPixelData` / `pixelData`, PNG save, `projection`, `exportImage`, `saveAs .kra`, `clone`; opening and rendering the Mao `.ora` | Default timeouts (D9), D10, whether 184-layer `create_layers` fits in 300 s | |
| Q12 | Can `kritarunner` import the plugin package and run commands headless? | D16 (headless live tests) | |
| Q13 | In batch mode: return values of `exportImage` / `saveAs`; does `fileName()` follow `saveAs` but not `exportImage`; does `close()` prompt after `setModified(False)`? | `save`, `close_document` | |
| Q14 | Is `layerStyleToAsl()` empty for a plain layer? Does `Node.save(..., exportRect)` write raw pixels or apply opacity? | Lint `layer-style`, D5 (`export_layers` path) | |
| Q15 | Blend mode ID strings (`add` vs `linear_dodge`); what an invalid ID does | `blend_mode` validation, lint `blend-mode` | |

### Q12 by hand: kritarunner

Quit Krita first. The plugin binds port 5678, and a second instance would
fail to bind.

```bash
# Is kritarunner usable at all?
/Applications/krita.app/Contents/MacOS/kritarunner --help

# Run a function from a module on Krita's Python path (the pykrita dir).
# Use a tiny module, e.g. pykrita/kmcp_runner_check.py, with
#   def run():
#       from krita import Krita
#       d = Krita.instance().createDocument(8, 8, "x", "RGBA", "U8", "", 72.0)
#       print("ok", Krita.instance().version(), d.width())
# (not the probe script itself: it runs main() on import).
/Applications/krita.app/Contents/MacOS/kritarunner -s kmcp_runner_check -f run
```

Record: does it start without a window, does `Krita.instance()` work, can
it create and save a document, and does our extension's `createActions`
run? The last one matters, because the HTTP server is started from
`createActions`.

## Not covered by the probe

- A layer *with* a layer style, for Q14's positive case. Make one by hand
  in the lint fixture ([acceptance.md](acceptance.md#fixtures)) and check
  that `layerStyleToAsl()` is non-empty there.
- Whether a modal dialog (an export option dialog, a "file changed on
  disk" prompt) can block the main thread during a command. If any probe
  hangs on a dialog, note which call caused it.
