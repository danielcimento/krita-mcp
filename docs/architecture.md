# Architecture

How krita-mcp is put together, and where new functionality goes.

## Components

```
MCP client ──stdio──▶ server.py (FastMCP) ──HTTP POST :5678──▶ kritamcp plugin ──libkis──▶ Krita
                      outside Krita                            inside Krita's Python
```

| Part | File(s) | Runs in | Job |
|---|---|---|---|
| MCP server | `server.py` | Its own Python (the repo's `.venv`) | Declares the MCP tools and their argument schemas, turns each call into `{"action", "params"}` and posts it to the plugin |
| Krita plugin | `krita-plugin/kritamcp/` + `kritamcp.desktop` | Krita's bundled Python (3.13 in Krita 5.3.4), with PyQt5 | HTTP listener, auth, command queue, and the command handlers that call Krita's Python API (`libkis`) |

Plugin modules (`krita-plugin/kritamcp/`):

| Module | Job |
|---|---|
| `__init__.py` | Registers `KritaMCPExtension` with Krita; nothing else. Outside Krita (`krita` not importable) it registers nothing |
| `extension.py` | `KritaMCPExtension`: in `createActions` makes the output dir, loads the token, starts the server thread and the 50 ms timer |
| `bridge.py` | `PaintRequestHandler` (Host/browser/token checks), `CommandQueue`, `ServerThread`, token loading. No token loaded means every authenticated request gets 401 |
| `dispatch.py` | `execute(command)`: action name → handler, any exception → `{"error": str(e)}` |
| `commands/` | Handlers: `paint.py` (and the brush state), `documents.py`, `history.py`; `_active.py` finds the active document/view/layer |
| `config.py`, `errors.py`, `paths.py`, `params.py` | Pure (no `krita`/PyQt5): constants, `CommandError`, `resolve_allowed_path`, `int_param`/`float_param`. Unit-tested in the repo venv |

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
4. The handler's return dict goes back to the HTTP thread and out as JSON.
   A dict containing `"error"` is sent with HTTP 500.

Consequences:

- Commands run strictly one at a time. A 40 s export blocks every other
  command, and the Krita UI too, for 40 s.
- `HTTPServer` is single-threaded, so `/health` also waits behind a long
  command.
- The wait limit exists twice: `get_result(timeout=120)` in the plugin and
  the `httpx` timeout in `server.py`. Both have to allow for the slowest
  command. When the plugin side times out, the command still runs later,
  and its result is never collected.

## Security model

See the README's *Security* section. In short: loopback only, shared
token from `~/.krita-mcp-token`, browser-originated requests refused, file
access limited to `ALLOWED_DIRS` (realpath-resolved, so `..` and symlinks
cannot escape) plus an extension allowlist, and size caps on canvases,
strokes and request bodies.

## Adding a command (current layout)

1. Add `cmd_<name>(params)` to the fitting module in
   `krita-plugin/kritamcp/commands/`. Validate inputs with `int_param`,
   `float_param` (`params.py`) and `resolve_allowed_path` (`paths.py`),
   and raise `CommandError` (`errors.py`) for bad input.
2. Add the action to the `if/elif` chain in `dispatch.execute()` and to the
   `/info` command list in `bridge.py`.
3. Add a `@mcp.tool()` wrapper in `server.py` and pass a larger `timeout`
   to `send_command` if the command can be slow.
4. Add a headless test calling `dispatch.execute` in `tests/krita/`
   (`make test-krita`).
5. Copy the plugin into Krita's `pykrita` directory and restart Krita.
   Krita does not reload plugin code while it runs.

The F001 buildout replaces steps 1–3 with a dispatch registry. See
[f001_buildout/plan.md § Target layout](f001_buildout/plan.md#target-layout).

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
