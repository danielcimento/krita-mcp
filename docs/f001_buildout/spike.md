# F001 API spike (Phase 0)

Several design choices depend on how Krita 5.3.4's Python API behaves, and
the type stub doesn't settle that. This phase answers those questions
before code depends on them.

## How to run

1. In Krita: **Tools → Scripts → Scripter**, then open
   [`spike/krita_api_probe.py`](spike/krita_api_probe.py) and **Run**.
2. Results print in Scripter and go to
   `~/krita-mcp-output/spike/probe-results.json`, rewritten after every
   probe. `probe-progress.txt` in the same folder names the probe that was
   running. If Krita hangs or crashes, that file says which probe caused it,
   and the JSON keeps everything before it. Add a probe's ID to `SKIP` to
   leave it out of a rerun.
3. Optionally set `RUN_LARGE = True` and run again for Q11. That needs
   about 2 GB of RAM and a minute or two.
4. Do Q12 by hand (below).
5. Fill in the *Result* column here, update
   [../krita-api-notes.md](../krita-api-notes.md) (change *[to verify]* to
   *[verified]* or correct the entry), and close the matching decisions in
   [decisions.md](decisions.md). Copy the JSON into `spike/` as
   `probe-results-run<N>.json` and commit it.

The script creates and closes its own `probe-*` documents and doesn't
touch open work. Each probe runs in its own `try`. A bug in one probe
records `"status": "error"` with a traceback and the run continues, so
fix the probe and rerun rather than reading anything into that result.

## Questions

Results are from run 2 (`spike/probe-results-run2.json`), with run 3
(`spike/probe-results-run3.json`) filling in the items marked *run 3*.
Run 3 repeated every run-2 result unchanged.

| # | Question | Decides | Result |
|---|---|---|---|
| Q0 | Krita and Python versions, numpy, byte order, scaling filter names | D5, `transform_layer` `resample` values | Krita 5.3.4.1, Python 3.13.5, **no numpy**, little-endian. Filters: Hermite, Bell, Lanczos3, Bilinear, Mitchell, NearestNeighbor, BSpline, Bicubic |
| Q1 | Which libkis calls create Krita undo steps? | D6, D10 | **Mixed.** Undoable: `addChildNode`, `remove`, `setBlendingMode`. Not undoable: `setName`, `setVisible`, `setOpacity`, `setInheritAlpha`, `setPixelData`, `move`, `setActiveNode`. Reorder: one undo only reverts the re-add. The undo after a non-undoable call reverted the setup's `addChildNode`, which shows that Edit → Undo hits earlier, unrelated steps. D6 is closed |
| Q2 | Is `Node.bounds()` exact or tile-aligned? | `bounds`, cropping, lint | **Exact**, and it shrinks to (0,0,0,0) when the pixels are cleared. Empty layer: (0,0,0,0), `hasExtents` false. Not clipped to the canvas: off-canvas pixels are kept and readable |
| Q3 | UUID format, lookup, persistence | D4, D10 B, `diff_documents` | Braced lowercase. Lookup works with or without braces. **Kept** by `clone()` and `.kra`, **not** by `.ora`. The probe's `clone_keeps_ids` / `same_ids` read `false` only because it left out the default `Background` layer: the Group and Alpha IDs match. ORA reopens with an empty `name()`. *Run 3:* Node `==` and `!=` work |
| Q4 | `createDocument` defaults, profiles, `Document ==` | `new_canvas`, D3 | Adds one opaque white **`Background`** paint layer. Default profile `sRGB-elle-V2-srgbtrc.icc`. 21 RGBA/U8 profiles. `==` finds the doc in `documents()`, `is` doesn't |
| Q5 | BGRA order and bit-exact round trips | D5, acceptance test 2 | **Confirmed.** BGRA. `setPixelData`/`pixelData` and QImage→PNG→QImage are bit-exact, including partial and zero alpha |
| Q6 | `move`, `scaleNode`, `rotateNode` semantics | `transform_layer` | `move` sets an **absolute** offset. `scaleNode(origin, w, h)`: bounds become w×h with `origin` fixed. `rotateNode` rotates about the **bounds' center**. *Run 3:* positive radians turn **clockwise on screen** |
| Q7 | `setVisible` vs `modified` flag; projection follows visibility; clone cost | D10 | `setVisible` does **not** set `modified`. Clone of a small doc: 1 ms. *Run 3, on a blank doc:* the projection **follows visibility** (alpha 255 → 0). D10 is closed: A |
| Q8 | Group projection: own opacity? clipping baked? | `merge_layers` | **Excludes** the group's own opacity and **includes** clipping baked in |
| Q9 | `Selection.cut`/`paste`; `setSelection(None)` | `selection_to_layer`, `select(none)` | `cut` clears the source and `paste` writes into the destination, with no new layer. **`setSelection` adds a root-level `Selection Mask` node.** `setSelection(None)` makes `selection()` return `None`. *Run 3:* the mask node **goes away** after deselecting, and `cut` puts `application/x-krita-selection` + `application/x-qt-image` on the **system clipboard** |
| Q10 | `Selection.grow` as dilation | lint `stray-alpha`, `trim_layers` | Works, with a roughly round kernel: 1 px grown by 4 gives 69 px in a 9×9 box. `setPixelData` works after `resize` |
| Q11 | Timings at 5800×8400 | D9, D10, `create_layers` budget | (`spike/probe-results-q11.json`) In-Krita pixel work is milliseconds: full `setPixelData` 39, `pixelData` 20, `projection` 17–27, **`clone` 4–10 ms (copy-on-write)**. File I/O dominates: QImage PNG save 1.7 s, `exportImage` 1.3 s, `saveAs .kra` 1.9 s, open Mao `.ora` 2.6 s. Mao: 222 nodes, empty `name()`. D9 confirmed; D10 switched to clone |
| Q12 | `kritarunner` headless | D16 | **Works** (2026-11-24, `spike/runner-result.json`). No windows; create, `setPixelData`, `saveAs`, reopen and pixel read-back all correct. Own profile (`~/Library/Application Support/kritarunner/`); imports only from its `pykrita/`; `PYTHONPATH` ignored; the function gets the arg list. The bridge can't start (no `createActions`), so headless tests call dispatch directly. D16 closed |
| Q13 | Batch-mode save, export, close | `save`, `close_document` | No dialogs. `exportImage`/`saveAs` return `True`. `exportImage` leaves `fileName()` alone, `saveAs` re-points it and clears `modified`. `close()` after `setModified(False)` returns `True` with no prompt |
| Q14 | `layerStyleToAsl()` on a plain layer | lint `layer-style` | `''` |
| Q15 | Blend mode IDs and invalid values | validation, lint | `add` and `linear_dodge` are distinct, valid IDs. **Any string is accepted**, `not-a-mode` included, so validation is ours. *Run 3:* an invalid ID renders like Normal (50% gray over white stays 128; the test can't tell Normal from Multiply) |
| Q16 | `Node.save` under app-wide batch mode | D5 | No dialog. Wrote a 10×10 PNG, **with the layer's opacity baked into the alpha** (200 → 100 at 50% opacity). So not usable as raw export. D5 confirmed |
| Q17 | Is `==` safe on a closed `Document` wrapper? | D3 (registry design) | *Run 3:* **safe**, and the closed doc no longer matches anything in `documents()` |
| Q18 | Can a removed node be re-inserted with its pixels, properties and UUID? | D6 (journal inverse for delete) | *Run 3:* **yes**: same UUID, pixels, bounds, opacity and parent. `nodeByUniqueID` returns `None` while it is out |

### Q12 by hand: kritarunner

Done (see the table and the run log). The GUI Krita can stay open:
`kritarunner` uses its own profile, never loads our extension, and opens
no window, so it never binds the port. The check module is
[`spike/kmcp_runner_check.py`](spike/kmcp_runner_check.py).

```bash
# Is kritarunner usable at all?
/Applications/krita.app/Contents/MacOS/kritarunner --help

# kritarunner imports only from ITS OWN profile's pykrita folder:
mkdir -p ~/Library/Application\ Support/kritarunner/pykrita
cp docs/f001_buildout/spike/kmcp_runner_check.py ~/Library/Application\ Support/kritarunner/pykrita/
/Applications/krita.app/Contents/MacOS/kritarunner -s kmcp_runner_check -f run hello
# → ~/krita-mcp-output/spike/runner-result.json
```

Record: does it start without a window, does `Krita.instance()` work, can
it create and save a document, and does our extension's `createActions`
run? The last one matters, because the HTTP server is started from
`createActions`.

## Run log

**2026-11-24, run 1 (Krita 5.3.4, macOS 26.3, arm64).** Krita hung on a
modal "PNG image" export-options dialog. Accepting or cancelling it
crashed Krita (`EXC_BAD_ACCESS` / `SIGSEGV`, address 0x10, main thread).
The files left in `~/krita-mcp-output/spike/` show Q3 and Q13 had finished
and `probe-nodesave.png` had been written. So the call was Q14's
`Node.save(path, …, InfoObject(), rect)` on a document with no batch mode
set. Other exports in the probe set document batch mode or use
`QImage.save`, and showed no dialog. Results were lost, because the
script only wrote them at the end.

Changes for run 2: results are saved after every probe, a progress file
is written, the `Node.save` check moved to Q16 (run last, under app-wide
batch mode), and `RUN_NODE_SAVE` / `SKIP` let it be left out.

**2026-11-24, run 2.** All 16 probes finished (results in
`spike/probe-results-run2.json`). Krita then segfaulted in the script's
cleanup, at address 0x10 again. The progress file read `finished Q16`,
and the cleanup's `complete` marker was missing. The cause was the
probe's own bug: Q13 closed its `victim` document, which was also in
the cleanup list, so cleanup called `setModified()` on a closed
document's wrapper. That gave the most important finding of the run: **calling a
method on a closed `Document` crashes Krita** (D3).

Changes for run 3: cleanup skips documents that are no longer open
(checked by `==` against `documents()`), and Q13 removes the document it
closes from the list. `blank=True` scratch documents drop the
`Background` layer. Probes fixed: Q3's node-equality check, Q7 on a
blank doc, Q6 records the rotation direction, Q9 checks the mask node
and clipboard after deselecting, Q15 renders an invalid mode. New: Q17
(`==` on a closed wrapper) and Q18 (re-inserting a removed node).

**2026-11-24, run 3.** Completed with no crash (progress file:
`complete`). Results in `spike/probe-results-run3.json`. All run-2 results
repeated. Every open question except Q11 (timings at Mao size, optional)
and Q12 (kritarunner, optional) is answered, and D3, D6 and D10 are
closed. Q11 feeds the default timeouts (D9). P1.5 can start with the
planned defaults, which get tuned once Q11 runs.

**2026-11-24, Q12 (by hand, then reproduced).** The user's first try
failed with `No module named 'kmcp_runner_check'`, for two reasons. The
module had never been created (my instructions only described it), and
`kritarunner` looks in its own profile, not the GUI Krita's `pykrita`.
Putting the module on `PYTHONPATH` didn't help: the path is logged but
not used. In `~/Library/Application Support/kritarunner/pykrita/` it
imported. It then failed with `run() takes 0 positional arguments but 1
was given` (kritarunner passes the arg list), and passed once the
signature was `run(args=None)`. The test module was removed from the
runner profile afterwards.

**2026-11-24, Q11** (`RUN_LARGE = True`, other probes skipped). Clean.
The surprise was `clone()`, effectively free even for Mao, which made the
clone-based render (D10 B) the better default.

## Not covered by the probe

- A layer *with* a layer style, for Q14's positive case. Make one by hand
  in the lint fixture ([acceptance.md](acceptance.md#fixtures)) and check
  that `layerStyleToAsl()` is non-empty there.
- Other modal dialogs that could block the main thread during a command,
  for example a "file changed on disk" prompt. If any probe hangs on a
  dialog, note which call caused it.
