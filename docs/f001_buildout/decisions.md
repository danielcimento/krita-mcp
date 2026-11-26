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
  The stored wrapper is used only in `==`, which is safe after the
  document closes (spike run 3, Q17).
- The `doc` parameter accepts: a handle; an absolute path that is the
  `fileName()` of exactly one open document; or nothing, meaning the active
  document (`no active document` is `not_found`).
- Handles live in memory only. They don't survive a Krita restart, and
  they aren't written into the file: stamping them into document
  annotations would mark the document modified and leak into saved files.

Settled in P1.6 (2026-11-24), `kritamcp/registry.py`:

- Handles count up from `doc-1` and are never reused in a process. A
  `doc` value is a handle if it looks like `doc-<n>`, a path if it is
  absolute (or starts with `~`), and anything else is `bad_request`.
- A path is compared after `os.path.realpath` with every open document's
  `fileName()` (Krita keeps the path as given, e.g. `/var/…` for
  `/private/var/…`). It only identifies an open document, so it isn't
  checked against the allowed roots. Two matches are `ambiguous` with
  `details.matches` listing their handles.
- The active document from `activeDocument()` is matched against
  `documents()` like any stored wrapper before use. Headless it is always
  `None`, so `doc` is effectively required there.
- The registry holding the wrapper keeps a headless document from
  `createDocument` alive (it is destroyed with its last reference
  otherwise), so a headless `new_canvas` leaves an open document.

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

Settled in P1.6 (2026-11-24), `kritamcp/refs.py`:

- A ref is tried in order: `"/"`, then `"$key"` (D11; only where the
  tool accepts batch keys), then UUID, then path (any unescaped `/`),
  then bare name. A layer literally named like a UUID, or whose name
  starts with `$`, is reached with a path: `/{…}`, `/$name`.
- Escapes: `\/` and `\\`. Any other backslash, or an empty component
  (`a//b`, `a/`), is `bad_request`. One leading `/` is optional, so
  `/Face` picks the top-level `Face` when a nested one shares the name.
- Matching is exact (case-sensitive, no trimming). A path collects every
  node that matches it in full, so a name repeated at an intermediate
  level fans out and still resolves if only one endpoint matches.
  `details.matches` lists IDs in stacking order, top to bottom.
- A result's `path` has no leading `/`, so a top-level layer's path is
  its bare name; prefix `/` to anchor it.

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

*(P1.7, 2026-11-25): the helper is `kritamcp/fileio.py`.* `batch_mode(doc,
app_wide)` is a context manager that sets the flags and restores their
previous values. `export_image` and `save_as` use the document flag;
`save_in_place` uses both; `open_document` uses the app-wide flag, which
`openDocument` passes on to the document while it loads (from Krita's
source; no prompt-producing file was tried). Each save or export
also checks that the file was written, since `saveAs` and `save` return
`True` without writing when the directory is missing.

## D6. Undo granularity

**Decided** (spike run 2, Q1; run 3, Q18 confirmed that a removed node
can be re-inserted with its UUID, pixels and properties, so the delete
inverse works as described).

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
  - *(P2.5, 2026-11-25)* Removing a node in Krita detaches its whole
    subtree: a group comes back empty unless its descendants are re-added
    too (Q18 only covered a single paint layer). Delete and move inverses
    therefore save the subtree's wrappers before removing and re-add them
    afterwards (krita-api-notes § Nodes).
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

Settled in P5.1 (2026-11-25), `kritamcp/batch.py`, `commands/history.py`:

- **Redo-capable inverses.** Applying an inverse returns its own reverse,
  an inverse of the same protocol (`node_ids`, `nbytes`, `check(doc)`):
  `RemoveNode` ↔ `ReinsertNode` (removing captures the node's place and
  subtree, so an undone create redoes with the same ids), `RestoreProps`
  returns the values it overwrote, `MoveBack` the forward move,
  `RestorePixels` the pixels it overwrote (read over the same rectangles
  first). `batch.rollback` ignores the return value. Each journal has an
  undo and a redo stack; `undo` applies the newest undo entry (inverses
  newest first) and pushes the reverses as one redo entry, `redo`
  mirrors it, and any new push clears the redo stack.
- **Preconditions.** `check(doc)` runs just before each inverse, against
  the state the inverses before it left; a reason (a layer gone, a
  re-insert's layer already back or its parent gone) is `conflict`
  naming the entry and the reason. **The entry stays on the stack**, so
  a user who put the layer back can retry. If an inverse fails after its
  check (Krita refused, a spill file is missing), the steps already
  applied are reversed again with the ops they returned, and the error
  keeps the inverse's `CommandError` code (else `krita_error`), with
  `details.applied` / `rolled_forward` / `rollback_errors`; the entry
  stays too. Only when that roll-forward fails as well is the journal
  cleared, since its entries no longer match the document.
- **Caps.** `max_actions` applies to each stack; `max_bytes` (memory) and
  `max_disk_bytes` (spill files, counted uncompressed; default 4 GB,
  read defensively from `config.current().journal`) cover both stacks
  together. Over the memory cap, pixel snapshots are spilled, the
  entries farthest from the present (the bottoms of the two stacks)
  first; still over a cap, the farthest entries are dropped. Detached
  layers can't be spilled and count against the memory cap. An entry
  that can't fit alone is not stored and its stack is cleared:
  `not_undoable` on the tool's result (or `not_redoable` on an undo
  whose reverse is too big).
- **Spill files, not PNGs under a state directory.** Pixel snapshots are
  internal state, so they are written as raw bytes compressed with
  `zlib` level 1, one file per patch, in a per-process
  `tempfile.mkdtemp(prefix="kmcp-journal-<pid>-")`. A file is deleted
  when its entry is applied, evicted, cleared or forgotten (the registry
  prunes a closed document's handle and forgets its journal; nothing is
  called on the entry's nodes), and the directory at process exit
  (`atexit`). kritarunner skips `atexit`, so making a spill dir also
  removes the ones whose pid no longer runs.
- **Painting tools** (stroke, fill, draw_shape, clear) push one
  `RestorePixels` entry per call over the rectangle they write (`clear`:
  the canvas, or for `"transparent"` the canvas ∪ the layer's bounds);
  their results only gain `warnings` when the call is not undoable.
- **`ui_undo` / `ui_redo`** take `doc`: when it isn't the active
  document, its view (`Krita.instance().views()`, `view.document() ==
  doc`) is shown with `window.showView(view)` (and `window.activate()`
  if needed) first; a document with no view is `conflict` (always so
  headless). Without `doc`, the active document's view. The result
  carries the "only partly covers agent edits" warning.

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
       "journal": {"max_actions": 20, "max_bytes": 1073741824},
       "lint": {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4}
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
- *(P1.4, 2026-11-24): details settled in `config.py` / `paths.py`.*
  - Root order: the output dir first (included even before it exists),
    then the file's `allowed_dirs`, then the env var. Duplicates after
    `realpath` are dropped silently. Besides `/` and the home directory,
    any ancestor of home (e.g. `/Users`) is refused too.
  - A symlinked config file is accepted if both the link and its target
    are owned by the user; the mode check applies to the target. A
    non-regular file (directory, FIFO) is ignored with an error.
  - Re-reading is keyed on the file's stat (mtime, size, mode, owner,
    inode, ctime, so a `chmod` is noticed), the env vars, and whether each
    requested root now resolves elsewhere or (stops) existing, so creating
    a configured directory later needs no restart. Bad values never
    raise: they go to `Settings.errors`. Unknown keys are reported and the
    rest is used; an invalid limit keeps its default.
  - A directory output equal to a root is treated like any other
    directory (allowed when empty or with `overwrite`). Containment is
    re-checked after `makedirs`; if a symlink race sent the directory
    outside, the call fails with `forbidden_path`.
  - `KRITA_MCP_CONFIG_FILE` in **Krita's** environment replaces the
    config file path (used by tests and scripted launches). Like
    `KRITA_MCP_ALLOWED_DIRS`, server.py can't set it, so it doesn't widen
    the boundary; the replacement file gets the same owner/mode checks.
  - `/health` (no token) reports `allowed_dirs`, `config_file`,
    `config_loaded`, `limits` and `config_errors`. Any local process can
    read the directory names; that is accepted, since the server is
    loopback-only and refuses browser requests. If loading the config
    fails unexpectedly, `/health` still answers, with the failure in
    `config_errors`.
- *(P4.2, 2026-11-25): the `lint` section* (`Settings.lint`,
  `DEFAULT_LINT`). `additive_mode` is `"either"`, `"add"` or
  `"linear_dodge"` (D17); `stray_alpha_below` an int from 1 to 255 and
  `stray_alpha_distance` an int from 1 to 64 (D14). Unknown sub-keys and
  bad values go to `errors`, and the default is kept. `krita_lint` copies
  the section into its snapshot's `settings`.

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
  pass `timeout_s` (up to 900). Spike Q11 confirmed these leave plenty of
  room. At Mao size (5800×8400), Krita itself is fast: full-canvas
  `setPixelData` takes 39 ms, `pixelData` 20 ms, `projection` 17–27 ms. The
  slow parts are file I/O: **PNG encoding ≈ 1.7 s** per full-canvas
  image (QImage, a solid fill, so real art will be slower), `exportImage`
  1.3 s, `saveAs .kra` 1.9 s, and opening the Mao `.ora` 2.6 s.
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

**Decided: B (clone), with A as fallback** (spike Q1, Q3, Q7, Q11).
Originally A. Q11 showed that `clone()` of the full Mao document takes
**10 ms**: it is copy-on-write, not "one full document copy". So B costs
almost nothing, and it never touches the user's document. That means no
canvas flicker, no visibility state to restore, and nothing left behind if
anything fails midway. Clones keep node UUIDs (Q3), so `only` / `hide`
refs map straight across. A is still safe (`setVisible` sets neither the
modified flag nor an undo step, and the projection follows it), and
stays as the fallback.

Run 2 confirmed it's safe to close the clone once done. It is never
touched after `close()` (D3). The headless suite (D16) must check that a
clone's projection follows visibility changes made in the clone. If that
fails, switch to A.

`render(only=…, hide=…)` must not leave visibility changed. There are two ways:

- **B (default):** `doc.clone()`, toggle visibility in the clone,
  `refreshProjection()` + `waitForDone()`, read the clone's
  `projection(region)`, and close the clone in `finally`.
- **A (fallback):** toggle in place, read the projection, and restore
  visibility and the `modified()` flag in `finally`. The canvas flickers
  while it runs.

Renders without `only` / `hide` skip both and read `projection()`
directly. The cost of `render` is dominated by PNG encoding (≈1.7 s at
full Mao size), so agents should pass `scale` < 1 for previews.

*Built with B (P3.4, 2026-11-25).* The headless check passes
(`tests/krita/test_render.py` `CloneTest`): hiding an opaque layer in a
clone takes the clone's projection from alpha 255 to 0 while the
original's stays 255, and after subset renders the original's projection,
every node's visibility, `modified()` and the number of open documents
are unchanged. A render whose `only` / `hide` change nothing reads the
projection directly. Real Mao art encodes slower than the solid fill
measured in Q11: a full render takes 4.0 s at the default PNG level,
1.6 s at level 1, and 0.37 s at scale 0.25.

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

Settled in P2.2 (2026-11-25), `kritamcp/batch.py`:

- A validation error keeps its code; the message gets a `specs[3]: `
  prefix and `details` gain `index` and `field`. A `CommandError` raised
  mid-apply also keeps its code (a conflict found while applying stays a
  `conflict`); any other exception becomes `krita_error`. Either way the
  rollback runs. The item that raised must clean up after itself: only the
  items before it are rolled back.
- Rollback keeps going when an inverse fails. `details.rollback_errors`
  lists the failures and the message says the document may be partly
  changed. `details.rolled_back` counts the inverses that ran cleanly.
- Inverses are `inverse(ctx)` and get the live document at call time; they
  never hold a `Document` wrapper (D3 crash hazard).
- Journal (D6): a call with no inverses adds no entry. An entry bigger
  than the byte cap on its own is not stored and the journal is cleared,
  since undoing older entries past an unrecorded change would not restore
  their state; `push` returns `False` so the tool can warn. Limits are read
  from config when a document's journal is created.

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

*(P4.2, 2026-11-25): the ten default rules are built in
`kritamcp/lint.py`, over the snapshot format in api-design.md § Snapshot
(P4.1/P4.2), with one hand-written snapshot per rule in
`tests/unit/snapshots/`.* Settled on the way: a reference group's contents
are skipped by every rule, but the bracket group itself is checked like any
group; the side-token spellings and the suggested standard name are listed
in api-design.md § Lint rules. The thresholds and `additive_mode` come
from the config's `lint` section (D7).

## D15. Painting tools stay, with `doc` and `layer`

**Decided.** The existing painting tools get optional `doc` and `layer`
parameters, structured results, and the BGRA helper. The plugin keeps
the current foreground color itself, so painting into a non-active
document doesn't depend on a view. No new painting features are added.

Settled in P1.6 (2026-11-24): the color is plugin-global, like the
brush, so `set_color` and `set_brush` take no `doc`. `set_color` stores
the color and also sets the active view's foreground when there is a
view (`view_updated`); no view is not an error. Painting uses the stored
color, else the view's foreground, else black. `set_brush` stores `size`
and `opacity` (the stroke defaults) and mirrors `size` to the view; a
`preset` still needs a view (`conflict`). The color a user picks in
Krita's UI after a `set_color` doesn't reach the agent's painting.

## D16. Testing approach

**Decided** (spike Q12, 2026-11-24).

- **Unit tests** (pytest, repo `.venv`): the pure plugin modules (`paths`,
  `config`, `params`, `refs`, `lint`, `manifest`) and `server.py`
  wrappers with the HTTP call mocked. The repo venv is Python 3.14, while the
  plugin runs on Krita's 3.13. Plugin code must stay 3.13-compatible, so
  don't use 3.14-only syntax or stdlib APIs. `pixels.py` and anything else
  needing PyQt5 is tested in the headless suite below, which has
  Krita's PyQt5. The venv doesn't get PyQt5. `requirements-dev.txt` is
  `pytest` + `Pillow` (Pillow is for comparing renders in live tests).
- **Live tests** (`tests/live/`, skipped unless `KRITA_MCP_LIVE=1`): go
  through `server.py`'s functions against a running Krita, using a
  scratch root added to the allowlist. The acceptance tests live here.
- **Headless** (`tests/krita/`, `make test-krita`): `kritarunner` runs
  Krita with no window, and creating, editing, saving and reopening
  documents all work (Q12). No window means the extension's
  `createActions` never runs and the HTTP bridge never starts. So this
  suite calls the dispatch layer directly, `dispatch.execute({"action": …,
  "params": …})`, which covers every handler without the GUI. It uses
  stdlib `unittest`, since Krita's Python has no pytest.
  - `kritarunner` uses its **own profile**,
    `~/Library/Application Support/kritarunner/`, and ignores
    `PYTHONPATH`. It imports `-s <module>` only from that profile's
    `pykrita/` folder, and calls `-f <function>` with the argument list.
  - `tools/install_test_runner.sh` symlinks a small shim,
    `kmcp_test_runner.py`, into that folder. The shim takes the repo path
    as its argument, puts `krita-plugin/` and `tests/krita/` on `sys.path`,
    runs the suite, writes a JUnit-style report into the output dir, and
    prints a pass/fail line. `make test-krita` runs
    `kritarunner -s kmcp_test_runner -f run "$PWD"`.
  - The headless suite is independent of the GUI Krita: separate
    profile, no port, no extension. It can run while the GUI Krita is open.
- **What runs where:** handler logic and pixel work in `tests/krita`,
  headless. The HTTP bridge, auth, the queue and timeouts, plus the
  end-to-end acceptance tests, in `tests/live` against the GUI Krita.
  Pure logic in `tests/unit`.
- **Import side effects:** `kritamcp/__init__.py` used to create the
  token file when imported. Headless tests import the package, so P1.2
  moved token creation into the extension's startup (`createActions`).
  `__init__.py` only registers the extension, and registers nothing when
  `krita` isn't importable, so pure modules import under pytest. Done in
  P1.2 (2026-11-24).

## D17. Additive blend: `add` and `linear_dodge` are one mode

**Decided** (2026-11-25, with the user; probe:
[spike/probe-results-blend.json](spike/probe-results-blend.json)).

Krita has two IDs for additive blending, `add` ("Addition") and
`linear_dodge` ("Linear Dodge"). The probe found them **byte-identical**
in rendering at U8, U16 and F32, so they are one mode under two names.
They differ only on export: `.psd` writes `add` as **Normal** (the
additive effect is silently lost) and `linear_dodge` as `lddg`; `.ora`
writes `add` as the standard `svg:plus` and `linear_dodge` as the
Krita-only `krita:linear_dodge`.

- **Lint** accepts both as the rig's Add by default: no finding. The
  config file can require one spelling, for pipelines whose consumer
  compares strings:
  ```json
  {"lint": {"additive_mode": "either"}}
  ```
  `"either"` (default), `"add"` or `"linear_dodge"`. With a specific
  spelling, the other one is a `blend-mode` finding with a fix hint.
  **umamo accepts both** (surveyed 2026-11-25): it reads the `.kra`'s
  `maindoc.xml` and maps `compositeop` case-insensitively, `add` and
  `linear_dodge` both to `LayerBlend.Add` → Cubism `Additive`
  (`umamo/module/format/.../kra/KraReader.kt` `parseCompositeOp`, tested in
  `KraSyntheticReaderTest`). So `"either"` is the right default. Its PSD
  reader knows only `lddg` for additive and reads Krita's `norm` (from an
  exported `add`) as Normal, which the export conversion below prevents.
  umamo has no `.ora` or PNG+manifest importer.
- **Export conversion.** `save` to `.psd` writes `add` layers as
  `linear_dodge`; `save` to `.ora` writes `linear_dodge` layers as `add`.
  The conversion is done on a `clone()` of the document (D10), so the
  user's document is unchanged, and the result lists the converted layers
  in `blend_conversions`. A `.kra → .psd → .kra` round trip therefore
  brings `add` layers back as `linear_dodge`: a rename with identical
  pixels. `.kra` saves are never converted. Lands with P2 (it needs the
  tree walk and `save`).

  *(P2.7, 2026-11-25): built as above, in `kritamcp/blendexport.py`.*
  Settled on the way: every node counts, groups included (PSD keeps a
  group's mode, so an `add` group would also be lost); with nothing to
  convert no clone is made; an **in-place** save of a `.psd` / `.ora`
  document that needs conversion exports the converted clone over its
  own file instead of `Document.save()` and clears `modified`, so the
  file holds the converted spelling while the open document keeps the
  user's. The clone is not in `documents()` (headless, verified), so the
  registry never sees it. Headless tests: the reopened `.psd`'s
  projection is byte-identical to the original's, and Krita's own export
  of the same document (no conversion) is not.
