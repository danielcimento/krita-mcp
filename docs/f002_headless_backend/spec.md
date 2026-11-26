# F002: headless Krita backend (feature request)

| | |
|---|---|
| Status | **Requested, not scheduled.** A nice-to-have; no plan or decisions yet |
| Raised | 2026-11-25, after F001 (see [../f001_buildout/plan.md](../f001_buildout/plan.md)) |
| Next step | The Phase 0 probe below, then a `plan.md` + `decisions.md` in this folder |

## Request

Let krita-mcp drive a **headless** Krita process that `server.py` starts
and owns, as an alternative to driving the user's open GUI Krita.

Today every tool call goes to the plugin inside the user's GUI Krita, over
HTTP on `localhost:5678`. That ties an agent's workflow to that one
process:

- Plugin code changes need the **user** to restart Krita before an agent
  can use them.
- A Krita crash (two GUI-only ones turned up during F001; see
  [krita-api-notes § Crash hazards](../krita-api-notes.md#crash-hazards))
  takes down the user's session and stops every agent until the user
  restarts it.
- Several agents share one Krita: commands run one at a time on its main
  thread, and they act on the same set of open documents and UI state
  (active document, foreground colour, selection).
- The user's own work and the agents' work are mixed in one app.

A headless backend would let `server.py` start, health-check and restart
its own Krita, with no user step, and keep agent crashes away from the
user's session.

**Not requested:** removing the headed mode. It stays the default, or at
least an option: watching an agent work in the GUI, or co-editing a
document with one, is useful and only possible headed.

## What is already known (from F001)

- **The command layer already runs headless.** F001's headless suite
  (`make test-krita`, 300+ tests) runs every command through
  `kritamcp.dispatch.execute` inside `kritarunner`
  (`/Applications/krita.app/Contents/MacOS/kritarunner -s <module> -f <function> [args…]`).
  Pixel work, saves, exports, clones, selections and the journal all work
  there.
- **What ties the plugin to the GUI is only its startup.** The HTTP bridge
  (`bridge.ServerThread`) and the 50 ms command timer are created in
  `KritaMCPExtension.createActions` (`krita-plugin/kritamcp/extension.py`),
  which Krita calls only when a window opens. `kritarunner` opens no
  window, so neither starts.
- `kritarunner` facts [krita-api-notes § Environment]:
  - It has its **own profile**, `~/Library/Application Support/kritarunner/`,
    and imports `-s <module>` only from that profile's `pykrita/`.
    `PYTHONPATH` is ignored. It calls the function with the argument list.
  - **Its exit code is always 0.** Callers judge success by output.
  - It passes the environment through to Python.
  - Startup takes 3–4 s.
  - **Two `kritarunner` processes on the same profile deadlock at startup**
    (`UNIQUE constraint failed: storages.location` in the resource
    database).
  - It doesn't conflict with a running GUI Krita.
- Headless behaviour differences [krita-api-notes]:
  - `activeDocument()` and `Document.activeNode()` are always None, so
    every call needs `doc`. The tools accept it.
  - There are no views, so `krita_ui_undo` / `krita_ui_redo`,
    `set_active_layer` (no effect), and `set_brush` presets don't apply.
  - A `createDocument` document dies when its last Python reference goes.
    The registry holds it.
  - `projection()` and pixel APIs behave the same, and images are never
    "busy" (the GUI's busy-image export refusal didn't occur).
- **Port.** `config.SERVER_PORT` is fixed at 5678 and `ALLOWED_HOSTS`
  (the Host-header check) is derived from it. A daemon running next to a
  GUI Krita needs its own port, and the server needs to know which one.
- **Memory.** The headless Mao suite peaked at about 6.5 GB RSS, so each
  daemon is a full Krita process.

## Open questions (the Phase 0 probe answers 1–4)

1. **Can a long-running `kritarunner` daemon serve the bridge?** The
   function `kritarunner` calls would start the bridge on another port and
   run the command queue on its main thread until told to stop. Does
   `kritarunner` stay alive while the function blocks? Do `QTimer` and
   `QThread` work there (does it run a Qt event loop, or must the function
   run one: `QEventLoop().exec_()`, or a `processEvents()` loop)?
2. **Can several daemons run side by side?** That needs separate
   profiles, since two on one profile deadlock. Does overriding `HOME`
   (or another variable Krita reads for its data location on macOS) give a
   daemon its own profile? What does a fresh profile cost on first start
   (resource database build)?
3. **Cost:** startup time to first command, idle memory, and memory after
   rebuilding Mao.
4. **Shutdown and crash:** does a clean stop leave no stale state? After a
   `kill -9`, does the next start recover (lock files, resource DB)?
5. **Lifecycle design** (for the plan, not the probe):
   - Should `server.py` spawn one daemon per MCP server process, or should
     several servers share one daemon? Per-agent daemons give isolation
     and parallelism; a shared one gives less memory and one queue.
   - How is a daemon chosen, configured (port, profile, allowed dirs),
     health-checked, restarted after a crash or when the plugin code
     changes, and stopped when the MCP client exits?
6. **Files open twice:** a `.kra` open in the user's GUI Krita and in a
   daemon has two writers. Is a lock file needed, a check of the other
   side's open documents, or only a documented convention?
7. **Modes:** a config switch (`backend: "gui" | "headless"`) or a
   per-call choice? How do tools that need a view report themselves in
   headless mode (`conflict`, as `krita_ui_undo` already does)?

## Phase 0 probe

Run it headless only; it never touches the user's GUI Krita. Follow
F001's probe conventions ([../f001_buildout/spike.md](../f001_buildout/spike.md)):
save results to JSON after each step, and record facts in
`krita-api-notes.md` tagged `[verified <date>]`.

1. **Daemon module.** Write `kmcp_daemon_probe.py` exposing
   `run(args)`, with `args[0]` the repo path, `args[1]` a port (e.g. 5690),
   and `args[2]` a results path. It should:
   - put `<repo>/krita-plugin` on `sys.path` and import `kritamcp`;
   - call `bridge.ensure_token()` and start `bridge.ServerThread(port)`.
     The bridge's Host check uses `config.ALLOWED_HOSTS`, built from port
     5678, so patch it to the probe's port for the probe;
   - drive the queue on the main thread with a `QTimer` calling the same
     logic as `KritaMCPExtension.process_commands`;
   - block, trying in this order: (a) `QEventLoop().exec_()`, (b) a loop of
     `QCoreApplication.processEvents()` plus a short sleep. It stops when a
     `POST {"action": "…"}` of a probe-only stop command arrives (register
     one with `@dispatch.command` inside the probe), or when a stop file
     appears;
   - write a results JSON with timings at each stage.

   Link it into the runner profile's `pykrita/` as F001 does
   (`tools/install_test_runner.sh` is the pattern), run
   `kritarunner -s kmcp_daemon_probe -f run <repo> 5690 <out.json>` in the
   background, and remove the link afterwards.
2. **Drive it** from the repo venv with `httpx`, with the token from
   `~/.krita-mcp-token` and `Host: 127.0.0.1:5690`:
   - `GET /health`;
   - a few commands: `new_canvas` → `create_layers` → `render` → `save` to
     a `.kra` → `close_document`;
   - two concurrent `GET /health` during a slow command (the threaded
     bridge should answer).

   Record the time to the first answered `/health` (startup) and the RSS
   (`ps -o rss`) idle and after the commands.
3. **Mao load** (optional, needs `KRITA_MCP_MAO_ORA`): rebuild Mao through
   the daemon with `tools/ora_to_manifest.py` + `create_layers(manifest=…)`,
   the same calls as `tests/live/test_acceptance.py::_rebuild_mao`, and
   record RSS and timings. Mao is third-party art: temp dirs only, deleted
   afterwards, never in the repo.
4. **Two daemons.** Start a second daemon (port 5691) with
   `HOME=$(mktemp -d)` (or the profile variable step 5 finds). Check both
   answer, and that the second doesn't deadlock. Record its first-start
   time (the fresh resource database). Then try two daemons on the **same**
   profile once, to confirm the known deadlock and see how it shows (a hang
   at startup, so use a timeout), and kill it.
5. **Profile location.** If `HOME` doesn't move the profile, find what
   does: Qt's `QStandardPaths` on macOS uses `~/Library/Application Support`
   from `HOME`; Krita may also read `KRITA_*` variables. Check the startup
   log for the resource path.
6. **Stop and crash.** Stop cleanly through the stop command, then `kill -9`
   a daemon mid-command and start it again on the same profile. Check that
   it starts, and look for leftover lock or temp files (the journal's
   `kmcp-journal-*` spill dirs included).

**Done when:** questions 1–4 have recorded answers, with timings and
memory figures, and the probe module and its JSON results are committed
under `docs/f002_headless_backend/spike/`. Then write `plan.md` (phases
and tasks) and `decisions.md` (lifecycle, ports, profiles, modes, file
locking), F001-style.

## Rough shape, if the probe succeeds

For orientation only; the plan decides.

- `kritamcp/daemon.py`: the `kritarunner` entry point. It starts the
  bridge on a given port and runs the queue, sharing the code
  `extension.py` uses (factor the startup out of `createActions`).
- `config`: the port becomes configurable, and the Host check follows it.
- `krita_mcp_server/backend.py`: spawns, health-checks and restarts the
  daemon (`kritarunner` path, profile dir, port, environment), and stops it
  at exit. `client.py` gets its URL from the backend.
- A config or environment switch for headed vs headless. Headless-
  inapplicable tools return `conflict` with a clear message.
- Tests: the headless suite already covers the commands. New tests cover
  the daemon lifecycle (start, health, crash, restart, two daemons) and
  run without the GUI.
