# Architecture

How krita-mcp is put together, and where new functionality goes.

## Components

```
MCP client ──stdio──▶ server.py (FastMCP) ──HTTP POST :5678──▶ kritamcp plugin ──libkis──▶ Krita
                      outside Krita                            inside Krita's Python
```

| Part | File(s) | Runs in | Job |
|---|---|---|---|
| MCP server | `server.py` + `krita_mcp_server/` | Its own Python (the repo's `.venv`) | `server.py` declares the MCP tools and their argument schemas. `krita_mcp_server/client.py` turns each call into `{"action", "params"}`, posts it to the plugin, checks the protocol version once per process, and unwraps the reply into a dict or a `ToolError` |
| Krita plugin | `krita-plugin/kritamcp/` + `kritamcp.desktop` | Krita's bundled Python (3.13 in Krita 5.3.4), with PyQt5 | HTTP listener, auth, command queue, and the command handlers that call Krita's Python API (`libkis`) |

Plugin modules (`krita-plugin/kritamcp/`):

| Module | Job |
|---|---|
| `__init__.py` | Registers `KritaMCPExtension` with Krita; nothing else. Outside Krita (`krita` not importable) it registers nothing |
| `extension.py` | `KritaMCPExtension`: in `createActions` caches Krita's version (for `/health`), makes the output dir and logs config errors, loads the token, starts the server thread and the 50 ms timer |
| `bridge.py` | `PaintRequestHandler` (Host/browser/token checks), `CommandQueue`, `ServerThread`, token loading. No token loaded means every authenticated request gets 401 |
| `dispatch.py` | The command registry (`@command(name, timeout, mutating)`, `get`, `actions`) and `execute(command)`, which returns the D2 envelope. Imports `commands/` to fill the registry |
| `commands/` | Handlers: `paint.py` (and the brush state), `documents.py`, `history.py`; `_active.py` finds the active document/view/layer |
| `config.py`, `errors.py`, `paths.py`, `params.py`, `refs.py`, `version.py` | Pure (no `krita`/PyQt5): fixed constants plus the user config (`~/.krita-mcp-config.json`, `KRITA_MCP_ALLOWED_DIRS`) as `config.current()`, re-read when it changes: allowed roots, output dir, canvas limits, errors for `/health`; `CommandError` and the code → HTTP status table; `resolve_allowed_path`/`resolve_output_dir`; `int_param`/`float_param`/`canvas_size`; layer-ref parsing; `PROTOCOL`/`PLUGIN_VERSION`. Unit-tested in the repo venv |

The server never touches pixels or files itself. All file I/O is done by
the plugin, so the plugin's path checks are the security boundary.

## Request lifecycle

1. `server.py` posts JSON to `http://localhost:5678` with the shared token
   in `X-Krita-MCP-Token`.
2. `bridge.PaintRequestHandler.do_POST` (HTTP thread) checks Host, browser
   headers, the token, Content-Type and body size, then pushes the command
   onto `CommandQueue` and blocks in `get_result()`.
3. A `QTimer` on Krita's **main thread** fires every 50 ms,
   pops **one** command, and runs `dispatch.execute()`. libkis calls must
   happen on the main thread, which is why the queue exists.
4. The handler returns a payload dict or raises `CommandError(code, message,
   details)`. `dispatch.execute` wraps it in the envelope
   `{"ok": true, "result": {...}, "elapsed_ms": n}` or
   `{"ok": false, "error": {"code", "message", "details"?}, "elapsed_ms": n}`;
   any other exception becomes `krita_error` (traceback in Krita's log).
   The HTTP thread sends it with 200 if `ok`, else the code's status
   (`errors.HTTP_STATUS`: `bad_request` 400, `not_found` 404, `conflict`
   409, `krita_error` 500, `timeout` 504, …; decisions.md D2). Requests
   refused before a command runs (401, 403, 413, 415, invalid JSON, unknown
   endpoint) keep the plain `{"error": "<string>"}` form.
5. `krita_mcp_server/client.call` returns `result` plus `elapsed_ms`, or
   raises `ToolError("<code>: <message>")`; `server.py` adds a one-line
   `summary`. Before the first command it checks that `/health` reports
   `protocol: 2`.

Consequences:

- Commands run strictly one at a time. A 40 s export blocks every other
  command, and the Krita UI too, for 40 s.
- `HTTPServer` is single-threaded, so `/health` also waits behind a long
  command.
- The wait limit exists twice: `get_result(timeout=120)` in the plugin
  (`bridge.RESULT_TIMEOUT_S`; the per-command timeouts in the registry are
  recorded but not used until P1.5) and
  the `httpx` timeout in `server.py`. Both have to allow for the slowest
  command. When the plugin side times out, the command still runs later,
  and its result is never collected.

## Security model

See the README's *Security* section. In short: loopback only, shared
token from `~/.krita-mcp-token`, browser-originated requests refused, file
access limited to the allowed roots from `config.current()` (the output
dir plus the config file's `allowed_dirs`; realpath-resolved, so `..` and
symlinks cannot escape) plus an extension allowlist, and size caps on
canvases (configurable), strokes and request bodies.

## Adding a command

1. Add a handler to the fitting module in
   `krita-plugin/kritamcp/commands/` and register it:

   ```python
   @command("fill", timeout=30, mutating=True)   # from ..dispatch
   def cmd_fill(params):
       x = int_param(params, "x", 0, -COORD, COORD)
       ...
       if not layer:
           raise CommandError("not_found", "No active layer")
       return {"x": x, "y": y}                     # the payload, no "status"
   ```

   Validate inputs with `int_param`, `float_param` (`params.py`) and
   `resolve_allowed_path` (`paths.py`), and raise `CommandError(code,
   message)` (`errors.py`) with a D2 code. `timeout` is 30 s for queries
   and 300 s for slow file I/O (D9). `mutating` marks commands that change
   a document or the set of open documents (not UI state such as the
   color). A new module must be imported at the bottom of `dispatch.py`.
   `/info` lists the registry, so nothing else needs updating plugin-side.
2. Add a `@mcp.tool()` wrapper in `server.py` that calls
   `client.call(action, params, timeout=…)` and returns the result with a
   one-line `summary`. Raise `ToolError("<code>: …")` for checks done
   server-side.
3. Add a headless test calling `dispatch.execute` in `tests/krita/`
   (`make test-krita`) and a wrapper test in `tests/unit/test_server_tools.py`.
4. Restart Krita (with a symlink install, see below; otherwise copy the
   plugin into Krita's `pykrita` directory first). Krita does not reload
   plugin code while it runs.

## Development install

Krita loads the plugin from its `pykrita` directory (macOS:
`~/Library/Application Support/krita/pykrita/`). `tools/install_plugin.sh`
symlinks `krita-plugin/kritamcp` and `kritamcp.desktop` from this
checkout into it, which removes the copy step:

```bash
tools/install_plugin.sh          # refuses if a copied install is in the way
tools/install_plugin.sh --force  # moves a copied install aside to <name>.bak-<timestamp>
```

Set `KRITA_PYKRITA_DIR` to install somewhere else. You still have to
restart Krita after a change.

The headless test suite runs in `kritarunner`, which has its own profile;
`tools/install_test_runner.sh` sets it up. See the README's Development
section.
