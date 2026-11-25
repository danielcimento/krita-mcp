# Hand-off prompt: F001 implementation orchestrator

Paste everything below the line into a fresh session started in
`/Users/daniel/krita-mcp`.

---

You are the orchestrator for building **F001** in this repo
(`/Users/daniel/krita-mcp`). F001 is a set of extensions that turn
krita-mcp, an MCP server plus a Krita plugin, into a tool for preparing
rig-ready layered Live2D artwork. Planning and an API spike are finished.
Your job is to carry out the plan: break it into tasks, delegate them to
subagents, check their work, keep the docs true, and commit as you go.

## Read first, in this order

1. `docs/f001_buildout/plan.md`: phases, commit-sized tasks (P1.1 …
   P6.2), target module layout, definition of done, test suites, risks.
   **This is your work queue.**
2. `docs/f001_buildout/decisions.md`: D1–D16, all decided. Treat them as
   settled. If implementation shows one is wrong, change it there with a
   dated rationale and tell the user. Don't drift from it silently.
3. `docs/f001_buildout/api-design.md`: the contract for every tool,
   manifest v1 and the lint rules. Built behaviour must match it. Update
   it in the same commit when they diverge for a good reason.
4. `docs/krita-api-notes.md`: verified Krita 5.3.4 API behaviour.
   **Read *Crash hazards* and *Undo* closely.**
5. `docs/architecture.md`, then `docs/f001_buildout/acceptance.md` and
   `docs/f001_buildout/spike.md` (the run log explains several design
   choices). `docs/f001_buildout/spec.md` is the original request.

Then read the code itself: `server.py` (355 lines) and
`krita-plugin/kritamcp/__init__.py` (889 lines). Neither has changed since
commit `7483197`; everything after it is docs.

## Ground rules

**Git**
- Work on the current branch, `local-fixes`. Commit after every task, so
  each commit is one P-numbered item or a clearly scoped part of one.
  **Never push**: there are no credentials. Leave the plugin installable
  and the existing 15 tools working at every commit.
- Follow your harness's commit-attribution instructions.

**Krita crash hazards.** These are segfaults, and `try` cannot catch them
(see krita-api-notes):
- Never call a method on a `Document` that may be closed. Check it is
  still open with `==` against `Krita.instance().documents()`, which is
  safe (D3).
- Never let a modal dialog open on the main thread. Every export or save
  goes through one batch-mode helper (D5). **Never call `Node.save`.** It
  ignores document batch mode, crashed Krita once, and bakes opacity into
  its output.
- Don't use `Selection.cut` / `copy` / `paste` in tools. They overwrite
  the user's system clipboard.

**Plugin code**
- It runs on Krita's **Python 3.13.5** with PyQt5 and **no numpy**. The
  repo `.venv` is Python 3.14 (FastMCP 4.2.6): don't use 3.14-only syntax
  or stdlib APIs in plugin code.
- No per-pixel Python loops over whole layers. Use `bytes` slicing and
  `translate`, `QImage` and `QPainter`, or `Selection.grow`. Krita's own
  operations take milliseconds at 48 MP. PNG encoding (≈1.7 s per full
  canvas) is the real cost (spike Q11).
- The plugin is the security boundary. Keep the realpath plus `commonpath`
  containment checks and the extension allowlist. Never let `server.py`
  widen the allowed roots (D7).

**Third-party art.** Mao (`~/krita-mcp-output/mao_pro_layered.*`) is a
third-party sample. Never commit its PNGs or anything derived from it.
Tests read it from disk via `KRITA_MCP_MAO_ORA`.

## What you can run, and what needs the user

You can run these yourself:
- `.venv/bin/pytest`: unit tests for the pure modules and the
  `server.py` wrappers with HTTP mocked.
- **Headless Krita** through
  `/Applications/krita.app/Contents/MacOS/kritarunner -s <module> -f run <args>`.
  This is the `make test-krita` suite (D16), which you build in P1.1.
  Facts verified on this machine:
  - `kritarunner` has its own profile, `~/Library/Application Support/kritarunner/`.
  - It imports `<module>` only from that profile's `pykrita/` folder.
    `PYTHONPATH` is ignored.
  - It calls the function with the argument list: `def run(args)`.
  - It opens no window, so the extension's `createActions` and the HTTP
    bridge never start. Headless tests call dispatch directly.
  - It runs fine alongside the user's GUI Krita. Each start takes 3–4 s,
    so run the whole suite in one start.

  Installing the test shim into the runner profile is fine. Tell the user
  the first time you do it.

Ask the user first, and batch these requests at phase exits:
- Anything touching **their GUI Krita**:
  - Their installed plugin is a *copy* of the `7483197` code at
    `~/Library/Application Support/krita/pykrita/kritamcp`. Switching it
    to the symlink install, or updating it, and restarting Krita are
    theirs to do.
  - A `server.py` process from their MCP client may be running. Leave it
    alone.
- Writing `~/.krita-mcp-config.json`. It changes what their running
  plugin allows.
- **Live tests** (`make test-live`, the acceptance tests). These need the
  GUI Krita running the plugin from this checkout. Give the user the exact
  commands and ask them to paste results, or ask whether you may run them
  once Krita is up.

## How to orchestrate

- **One task per subagent.** Brief each one with: the P-number and its
  text from plan.md; the decisions and api-design sections that apply,
  quoted or referenced by heading; the files it may touch; the tests it
  must add; the definition-of-done checklist from plan.md; and the
  hazards above that apply. Ask for a summary of changes and test output,
  not file dumps.
- **Review before committing.**
  - Read the diff.
  - Run `pytest`, plus the headless suite once it exists.
  - Check the work against api-design and the hazards: no `Node.save`, no
    per-pixel loops, no method calls on stale `Document` wrappers, exports
    in batch mode.
  - Then commit, or have the subagent commit after you approve.
- **Parallelism.** Most Phase 1 tasks touch the same files (`dispatch`,
  `bridge`, `server.py`), so run them in sequence. Safe to run in
  parallel, in separate worktrees, then merge:
  - pure modules with no shared edits: `config.py` + `paths.py` (P1.4),
    `refs.py` (P1.6);
  - `lint.py` rules over snapshots (P4.2), once Phase 1's layout exists;
  - `tools/ora_to_manifest.py` (P2.6).
- **Keep the docs true as you go.**
  - Tick plan.md checkboxes and update its Status row.
  - Record new API facts in krita-api-notes, tagged `[verified <date>]`.
  - Record decision changes in decisions.md.
  - Keep api-design in step with the code.
  - Update the README tool table at the end of Phase 4.
- **Checkpoints with the user:**
  - at the end of each phase: what landed, test results, and any
    install/restart/live-test requests;
  - at **P0 complete (end of Phase 4)**: stop for review before starting
    Phase 5.

## Known details that are easy to get wrong

- `createDocument` adds an opaque white `Background` paint layer.
  Today's `new_canvas` adds a second layer on top of it (see api-design
  for the fix).
- `setBlendingMode` accepts any string, invalid ones included. Validate
  against the plugin's own list. `add` and `linear_dodge` are distinct.
- `Document.setSelection` adds a root-level `Selection Mask` node. Skip
  it in listings, refs and lint.
- Krita's undo covers only some libkis calls. `addChildNode`, `remove` and
  `setBlendingMode` are undoable; renames, visibility, opacity, clipping,
  `setPixelData` and `move` are not. Hence the plugin-side journal (D6).
  Removed nodes can be re-inserted intact (Q18).
- UUIDs survive `.kra` and `clone()` but not `.ora`. `Node.bounds()` is
  exact and not clipped to the canvas. `move()` is absolute.
  `rotateNode` turns clockwise on screen about the bounds' center. A
  group's projection bakes clipping but not the group's own opacity.
- `clone()` is copy-on-write (10 ms for Mao). Subset renders use it
  (D10). A headless test must confirm that a clone's projection follows
  visibility changes made in the clone.
- Mao has 17 duplicated names, mostly a group and a layer sharing a name.
  Manifests link parents by `$key`, and acceptance test 1 compares the
  multiset of name paths.
- Importing `kritamcp` currently creates the token file. P1.2 moves that
  into extension startup, so headless tests can import the package.

## Small open items to settle during the work

- **ORA `isolation` → `pass_through` (P2.6):** compare with the group
  flags in `~/krita-mcp-output/mao_pro_layered.kra`, which Krita saved from
  the same `.ora`.
- **Lint fixture (P4.3):** try building it headless. libkis has
  `createFilterLayer`, and `setLayerStyleFromAsl` can give the
  `layer-style` rule a positive case. Fall back to a Scripter script the
  user runs.
- **Blend modes:** whether the rig's "Add" should map to Krita `add` or
  `linear_dodge` is an umamo question. Lint reports `linear_dodge`
  with a hint (api-design). Raise it with the user and don't decide it.

## First actions

1. Read the docs above and the two source files.
2. Run `git log --oneline` and `.venv/bin/pytest`. There are no tests yet,
   so expect "no tests ran".
3. Start **P1.1** (scaffolding, including the headless runner shim and
   `Makefile`), then **P1.2** (module split, no behaviour change, plus
   tests that pin current path-check behaviour).
4. Continue through Phase 1, then check in with the user at its exit.
