# F001 decisions

The choices the spec leaves open, or that the existing code forces. Each
entry has a status:

- **Decided**: build it this way.
- **Provisional**: build it this way unless the API spike
  ([spike.md](spike.md)) shows it can't work.
- **Open**: needs the spike result or the user's call before work starts.

---

## D1. Tool names keep the `krita_` prefix

**Decided.** Every tool, old and new, is `krita_<verb>`:
`krita_list_layers`, `krita_create_layers`, `krita_lint` and so on.

- The existing 15 tools already use the prefix, so nothing is renamed and
  current client configs keep working.
- MCP clients often load several servers into one tool namespace, and
  `save`, `render` or `select` would collide.

Plugin action names (the `action` field on the wire) have no prefix:
`list_layers`.

## D2. Structured results and errors

**Decided.**

*Success.* Every tool returns a JSON object containing at least:

```json
{"summary": "Created 3 layers in 'mao'", "elapsed_ms": 412, "...": "tool-specific fields"}
```

*Failure.* The tool raises `fastmcp.exceptions.ToolError`, so the client
sees an MCP error result (`isError: true`) rather than a success whose
text starts with "Error:". The message has the form `"<code>: <message>"`,
and any `details` are appended as JSON.

The plugin's response changes accordingly:

```json
{"ok": true,  "result": {...}, "elapsed_ms": 412}
{"ok": false, "error": {"code": "not_found", "message": "No layer 'Head/Eyes'", "details": {...}}}
```

| Code | HTTP | When |
|---|---|---|
| `bad_request` | 400 | Invalid or missing parameter |
| `not_found` | 404 | Unknown document handle, layer ref or file |
| `ambiguous` | 409 | A name ref matches more than one layer. `details.matches` lists the IDs |
| `conflict` | 409 | The state doesn't allow it: closing a modified doc without `discard`, writing into a non-paint layer, an output dir that is not empty |
| `forbidden_path` | 403 | Path outside every allowed root, or a disallowed extension |
| `too_large` | 413 | A canvas, region or batch over its limit |
| `timeout` | 504 | Execution exceeded its timeout (the command may still finish later; see D9) |
| `krita_error` | 500 | Krita returned failure (`saveAs` returned `False`, …) or an unexpected exception |

Auth and transport errors (401, 403 for host/browser, 413 for body size,
415) keep their current form. They happen before a command runs.

## D3. Document handles

**Decided.**

- The plugin issues handles of the form `doc-<n>` (`doc-1`, `doc-2`, …) and keeps
  a registry from handle to `Document`. Lookups compare with
  `Document.__eq__`, because each libkis call returns a new wrapper.
- Every document listed by `list_documents` gets a handle, including
  documents the user opened by hand. One document never gets two handles.
- Handles are pruned when their document is no longer in
  `Krita.instance().documents()`. A stale handle gives `not_found`.
- **Liveness check before every use.** Calling a method on a closed
  document's wrapper crashes Krita (spike run 2). So the registry checks
  `any(stored == d for d in app.documents())` on every lookup, and only
  ever calls methods on the live wrapper from `documents()` that matched.
  The stored wrapper is used only in `==`. Whether `==` is safe on a
  closed wrapper is probe Q17. If it isn't, the registry keeps no
  `Document` objects, only a fingerprint (`fileName()` + creation
  order), and re-matches against `documents()` on each call.
- The `doc` parameter accepts: a handle; an absolute path that is the
  `fileName()` of exactly one open document; or nothing, meaning the active
  document (`no active document` is `not_found`).
- Handles live in memory only. They don't survive a Krita restart, and
  they aren't written into the file: stamping them into document
  annotations would mark the document modified and leak into saved files.

## D4. Layer references

**Decided.**

A layer reference (`id`, `parent`, `above`, … in every tool) is one of:

1. **UUID**: `QUuid` text, with or without braces, any case. This is the
   canonical form, and every result returns `id` in braced lowercase form.
2. **Name path**: contains `/`, anchored at the document root:
   `Head/Eyes/Eyeball L`. Write `\/` for a literal slash in a name.
3. **Bare name**: no `/`. It is matched against every layer in the
   document. Documents that follow the conventions have unique names, so
   this is the convenient form.

Name forms that match more than one node fail with `ambiguous` and list
the matching IDs. The document root is `"/"` (valid only as a `parent`).

UUIDs are only stable as far as the file format keeps them. Verified in
spike run 2: `.kra` save and reopen and `Document.clone()` keep node
UUIDs, and `.ora` does not. `.psd` is assumed not to. Agents should treat
IDs as valid for one document session plus `.kra` round trips.
`nodeByUniqueID` accepts IDs with or without braces.

The document's global selection appears as a root-level
`selectionmask` node named `Selection Mask` (spike Q9). Ref resolution,
`list_layers` and lint skip it, and `get_document_info.selection`
reports it instead.

## D5. Pixel I/O uses QImage, with no numpy

**Decided.**

Krita's bundled Python has PyQt5 but no numpy (checked in the 5.3.4
bundle). All pixel I/O goes through one module, `pixels.py`:

- **PNG → layer:** `QImage(path)` → `convertToFormat(Format_ARGB32)` →
  bytes (BGRA on little-endian) → `setPixelData`.
- **Layer → PNG:** `pixelData` → `QImage(bytes, w, h, 4*w, Format_ARGB32)` → `save(path, "PNG")`.
- **Whole-buffer operations** (alpha extraction, thresholds, histograms,
  "over" compositing for `import_png`) use `bytes` slicing and
  `translate`, or `QPainter` composition modes. There are no per-pixel
  Python loops on large buffers.
- Only 8-bit RGBA documents are supported for pixel tools. Others fail
  with `conflict` and name the document's color model and depth.
  Conventions require 8-bit RGBA anyway.
- If numpy can be imported, `pixels.py` may use it as a fast path behind
  the same functions. Its results have to be identical.

`Node.save(..., exportRect)` is the alternative the spec mentions for
`export_layers`. **We don't use it.** On the first probe run it ignored
document batch mode, opened the PNG export dialog, and crashed Krita when
the dialog was dismissed (see [spike.md § Run log](spike.md#run-log)).
Run 2 showed that app-wide batch mode suppresses the dialog, but also
that `Node.save` **bakes the layer's opacity into the alpha**, so its
output isn't the layer's raw pixels. The `pixelData` → QImage path is
bit-exact (Q5) and is the only path.

All export calls, `exportImage` and `saveAs` included, go through one
helper that sets the document's batch mode, and the app-wide one when
needed, in `try`/`finally`.

## D6. Undo granularity

**Decided** (spike run 2, Q1). Re-inserting a removed node, which the
delete inverse needs, is checked by Q18.

Krita 5.3.4's Python API has no undo-macro call, so Krita's own undo
commands can't be grouped. Q1 found that undo coverage is **mixed**:
`addChildNode`, `remove` and `setBlendingMode` push undo steps, while
`setName`, `setVisible`, `setOpacity`, `setInheritAlpha`, `setPixelData`
and `move` don't. A reorder takes two steps. So after any agent batch,
Krita's Edit → Undo is unpredictable: it skips some of the agent's
changes and can revert earlier, unrelated ones.

Agents therefore get an undo they control, built on the batch executor
(D11):

- Every mutating batch tool records the inverse of each step it applies.
  It needs these anyway to roll back when a batch fails halfway.
  - create → remove the node.
  - delete → keep the detached `Node` alive (libkis wrappers hold a
    reference) and re-insert it at its old parent/position.
  - move → move back.
  - property update → restore the old values.
  - pixel write → restore the old pixels of the affected rectangle.
    These are kept in memory up to a cap, and in PNGs under the plugin's
    state directory beyond it.
- After a successful batch the inverse list goes onto a per-document
  **agent journal**. `krita_undo(doc)` pops and applies the newest entry,
  so one call reverts one agent action.
- The journal keeps the last 20 actions or 1 GB of pixel snapshots,
  whichever is smaller (configurable).
- Limits, to document: the journal doesn't know about edits made by hand
  in the UI. Undoing an agent action after the user painted on the same
  layer overwrites their strokes in that rectangle. `krita_undo` refuses
  entries whose nodes no longer exist.
- Krita's own UI undo stays available as `krita_ui_undo` / `krita_ui_redo`
  (the current `edit_undo` / `edit_redo` action triggers), aimed at the
  doc's view.

To document in the tool docs and README:

- Agent edits are only partly in Krita's undo history. After agent
  changes, Edit → Undo (and `krita_ui_undo`) may skip them or revert an
  unrelated earlier step. This affects the existing painting tools today.
- Applying a journal inverse uses the same libkis calls, so it also
  leaves partial entries on Krita's undo stack (a re-insert does, a
  rename doesn't).
- If Q18 shows that a removed node can't be re-inserted with its pixels,
  the delete inverse falls back to keeping a `duplicate()` made before the
  removal. That keeps pixels and properties, but the UUID changes, and the
  result reports the new ID.

## D7. Allowed roots come from config

**Decided.**

- The **plugin** reads the roots. The plugin is the security boundary, and
  the MCP server cannot widen it.
- Sources, combined:
  1. `CANVAS_OUTPUT_DIR` (`~/krita-mcp-output`), always.
  2. `~/.krita-mcp-config.json`, next to the token file:
     ```json
     {
       "allowed_dirs": ["~/umamo-art", "~/projects/mao"],
       "limits": {"max_canvas_dim": 16384, "max_canvas_pixels": 67108864},
       "journal": {"max_actions": 20, "max_bytes": 1073741824}
     }
     ```
  3. `KRITA_MCP_ALLOWED_DIRS` (`os.pathsep`-separated) in **Krita's**
     environment. On macOS, apps started from the Dock or Finder don't see
     shell variables, so the config file is the documented way and the
     variable is for scripted launches.
- The file is re-read whenever its mtime changes, so no restart is needed.
  It is ignored, with an error in `krita_health`, if it is group- or
  world-writable or not owned by the user, the same as the token file.
- Root checks: each root is `expanduser`'d and `realpath`'d. It must be an
  absolute, existing directory. `/` and the home directory itself are
  refused as too broad. Invalid roots are skipped and reported by
  `krita_health`.
- Path resolution keeps the current algorithm: relative paths resolve
  against the output dir, `realpath` runs before the containment check,
  and `commonpath` does the containment test. The image extension
  allowlist is unchanged. `.json` is allowed only for the
  manifest that `export_layers` writes and `create_layers` reads.
- Directory outputs (`export_layers`) must resolve inside a root. The
  directory is created if missing. It must be empty unless `overwrite: true`.

## D8. Size limits

**Decided.**

| Limit | Now | New | Why |
|---|---|---|---|
| `MAX_CANVAS_DIM` | 8192 | **16384** | Mao is 5800×**8400**. The spec's example (8192×8192) still refuses it, because the per-side limit applies |
| `MAX_CANVAS_PIXELS` | 16.8 MP | **67.1 MP** (8192²) | Mao is 48.7 MP. A full-canvas 8-bit RGBA buffer at the cap is 268 MB |
| `MAX_BODY_BYTES` | 5 MB | 5 MB | Pixels never travel inline |
| Batch size | none | 2000 specs per call | Bounds one request's main-thread time |

Both canvas limits can be overridden in the config file. `get_color_at`
and other coordinate checks use the document's real size, not
`MAX_CANVAS_DIM`.

## D9. Timeouts and long calls

**Decided.**

- Each command declares a default timeout in the dispatch registry: 30 s
  for queries, 300 s for save, export, render and batches. A caller may
  pass `timeout_s` (up to 900).
- `server.py` sets the `httpx` timeout to the command timeout plus 15 s,
  so the plugin's `timeout` error arrives before the transport gives up.
- When the plugin's wait times out, it marks the command ID abandoned. The
  main thread drops the result of an abandoned command, and skips the
  command if it hasn't started yet. Today such results pile up in
  `CommandQueue.results` forever.
- The HTTP server becomes a `ThreadingHTTPServer`, with the command
  counter under the queue lock. `/health` then answers while a long command
  runs, and reports `{"busy": {"action", "elapsed_ms"}}`. Execution stays
  one at a time on the main thread.
- Every result carries `elapsed_ms`, measured on the main thread.

## D10. How `render` hides layers

**Provisional, pending spike Q7 (run 3).** Run 2 established that
`setVisible` sets neither the modified flag nor an undo step, and that
`clone()` keeps UUIDs. Both options remain viable. Run 2's
projection check was spoiled by the white `Background` layer that
`createDocument` adds, so run 3 repeats it on a blank document.

`render(only=…, hide=…)` must not leave visibility changed. There are two ways:

- **A (default):** toggle visibility in place, `refreshProjection()` +
  `waitForDone()`, read `projection(region)`, and restore visibility and
  the `modified()` flag in `finally`. It is cheap. The canvas flickers
  while it runs.
- **B:** `doc.clone()`, toggle in the clone, read the clone's projection,
  and close the clone. The original is untouched, but memory and time are
  about one full document copy.

Use A unless the spike shows that `setVisible` creates undo steps or that
the modified flag can't be restored. In either case use B.

## D11. Batch semantics

**Decided.**

- **Validate first, then apply.** The whole batch is resolved and
  validated before any change: refs, parents, PNG headers, names and
  limits. Any error fails the call with nothing changed, and
  `details.index` names the bad item.
- **Apply in order.** Items run in list order. Within one call, later
  items may refer to layers created by earlier ones through `key` (see
  `create_layers`).
- **Roll back on failure.** If applying fails halfway, a Krita error
  rather than a validation error, the recorded inverses run in reverse
  and the error reports `details.applied` / `details.rolled_back`.
- **`dry_run: true`** runs validation only and returns what would happen.
- **One journal entry per call.** See D6.

## D12. Plugin split into modules

**Decided.** At 889 lines with an `if/elif` dispatcher, the plugin would
pass 4000 lines with the new tools. It becomes a package with a dispatch
registry. Modules that don't import `krita` can be unit-tested outside
Krita. See [plan.md § Target layout](plan.md#target-layout).

## D13. Export manifest = create_layers input

**Decided.** The `manifest.json` that `export_layers` writes can be fed
back to `create_layers`. This makes acceptance test 2 one call, and gives
external tools one format to read and write.

## D14. Lint runs on a snapshot

**Decided.** `krita_lint` collects a snapshot inside Krita: the tree,
node properties, the doc's color info, and per-layer alpha statistics for
paint layers. It then runs the rules as pure functions over that snapshot
(`lint.py`). The rules can be unit-tested with hand-written snapshots, no
Krita needed.

The default rule set is exactly the spec's list. The extra checks from
CONVENTIONS (nesting depth, Title Case, blend-mode suffix, pose
visibility, reference groups) are in an opt-in `conventions` set. Details
are in [api-design.md § Lint rules](api-design.md#lint-rules).

## D15. Painting tools stay, with `doc` and `layer`

**Decided.** The existing painting tools get optional `doc` and `layer`
parameters, structured results, and the BGRA helper. The plugin keeps
the current foreground color itself, so painting into a non-active
document doesn't depend on a view. No new painting features are added.

## D16. Testing approach

**Provisional, pending spike Q12 (kritarunner).**

- **Unit tests** (pytest, repo `.venv`): the pure plugin modules (`paths`,
  `config`, `params`, `refs`, `lint`, `manifest`) and `server.py`
  wrappers with the HTTP call mocked. `pixels.py` tests run when PyQt5 is
  installed in the venv (add it to a `requirements-dev.txt`).
- **Live tests** (`tests/live/`, skipped unless `KRITA_MCP_LIVE=1`): go
  through `server.py`'s functions against a running Krita, using a
  scratch root added to the allowlist. The acceptance tests live here.
- **Headless:** if the spike shows `kritarunner` can import the plugin
  package and run commands without a window, add a `make test-krita`
  target that runs the live suite without a GUI.
