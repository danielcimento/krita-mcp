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

The server never touches pixels or files itself. All file I/O is done by
the plugin, so the plugin's path checks are the security boundary.

## Request lifecycle

1. `server.py` posts JSON to `http://localhost:5678` with the shared token
   in `X-Krita-MCP-Token`.
2. `PaintRequestHandler.do_POST` (HTTP thread) checks Host, browser
   headers, the token, Content-Type and body size, then pushes the command
   onto `CommandQueue` and blocks in `get_result()`.
3. A `QTimer` on Krita's **main thread** fires every 50 ms,
   pops **one** command, and runs `execute_command()`. libkis calls must
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

1. Add `cmd_<name>(self, params)` to `KritaMCPExtension` in
   `krita-plugin/kritamcp/__init__.py`. Validate inputs with `int_param`,
   `float_param` and `resolve_allowed_path`, and raise `CommandError` for
   bad input.
2. Add the action to the `if/elif` chain in `execute_command()` and to the
   `/info` command list.
3. Add a `@mcp.tool()` wrapper in `server.py` and pass a larger `timeout`
   to `send_command` if the command can be slow.
4. Copy the plugin into Krita's `pykrita` directory and restart Krita.
   Krita does not reload plugin code while it runs.

The F001 buildout replaces steps 1–3 with a dispatch registry and splits
the plugin into modules. See
[f001_buildout/plan.md § Target layout](f001_buildout/plan.md#target-layout).

## Development install

Krita loads the plugin from its `pykrita` directory (macOS:
`~/Library/Application Support/krita/pykrita/`). Symlinking it there
removes the copy step:

```bash
ln -s "$PWD/krita-plugin/kritamcp" ~/Library/Application\ Support/krita/pykrita/kritamcp
ln -s "$PWD/krita-plugin/kritamcp.desktop" ~/Library/Application\ Support/krita/pykrita/kritamcp.desktop
```

You still have to restart Krita after a change.
