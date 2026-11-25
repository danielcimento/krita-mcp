# Krita MCP Server

Let AI paint in [Krita](https://krita.org/) via the [Model Context Protocol](https://modelcontextprotocol.io/).

This bridge allows Claude (or any MCP client) to create canvases, paint strokes, draw shapes, export images, and more — all inside a running Krita instance.

## How It Works

Two components:

1. **Krita Plugin** (`krita-plugin/`) — A Python plugin that runs inside Krita, exposing an HTTP server on `localhost:5678`. It receives paint commands and executes them on Krita's main thread via a command queue.

2. **MCP Server** (`server.py`) — A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes painting tools to any MCP client. It translates MCP tool calls into HTTP requests to the Krita plugin.

```
MCP Client (Claude, etc.)  ←→  MCP Server (server.py)  ←→  Krita Plugin (HTTP on :5678)  ←→  Krita
```

## Setup

### 1. Install the Krita Plugin

Copy the plugin files to your Krita plugins directory:

| OS | Path |
|----|------|
| Windows | `%APPDATA%\krita\pykrita\` |
| Linux | `~/.local/share/krita/pykrita/` |
| macOS | `~/Library/Application Support/krita/pykrita/` |

Copy both:
- `krita-plugin/kritamcp/` (the folder with `__init__.py`)
- `krita-plugin/kritamcp.desktop`

Then in Krita: **Settings → Configure Krita → Python Plugin Manager → Enable "Krita MCP Bridge"** and restart Krita.

### 2. Install the MCP Server

```bash
pip install -r requirements.txt
```

Or with a virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 3. Configure Your MCP Client

Add to your MCP client config (e.g., Claude Desktop's `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "krita": {
      "command": "python",
      "args": ["/path/to/server.py"]
    }
  }
}
```

If using a virtual environment:
```json
{
  "mcpServers": {
    "krita": {
      "command": "/path/to/.venv/Scripts/python",
      "args": ["/path/to/server.py"]
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `krita_health` | Check if Krita is running with the plugin active |
| `krita_list_documents` | List open documents with their `doc` handles, size, path, modified and active flags |
| `krita_get_document_info` | One document's size, resolution, color space, layer counts, nesting depth and selection |
| `krita_new_canvas` | Create a new RGBA canvas (width, height, background color or `"transparent"`, color profile, resolution); returns its `doc` handle |
| `krita_set_color` | Set foreground paint color (hex); kept by the plugin, so it works without a window |
| `krita_set_brush` | Set brush preset, size, and opacity |
| `krita_stroke` | Paint a stroke through a list of [x, y] points |
| `krita_fill` | Fill a circular area at a point |
| `krita_draw_shape` | Draw rectangle, ellipse, or line |
| `krita_get_canvas` | Export canvas to PNG (for AI to see progress) |
| `krita_save` | Save in place (no path), save as `.kra` (the document moves there), or export to PNG/JPG/…; returns the resolved path and file size |
| `krita_close_document` | Close a document by handle; refuses unsaved changes unless `discard=true` |
| `krita_list_layers` | The layer tree as a flat list, top to bottom (Layers-docker order): id, name, path, type, parent, children, visibility, opacity, blend mode, locks, bounds (exact, may extend past the canvas). Optional `root` (a subtree), `depth`, `fields`; per-type `counts` |
| `krita_create_layers` | Create groups and paint layers in one all-or-nothing call: list siblings bottom to top; `key` / `"$key"` to nest in layers made earlier in the call; `above`; visibility, opacity (0–1), blend mode, locks, clipping, pass-through; `png` + `x`/`y` for a layer's pixels. Unknown keys fail; `dry_run` validates only |
| `krita_undo` / `krita_redo` | Undo/redo actions |
| `krita_clear` | Clear a layer to a solid color, or `"transparent"` |
| `krita_get_color_at` | Eyedropper — sample color at a pixel |
| `krita_list_brushes` | List available brush presets |
| `krita_open_file` | Open an existing .kra, .ora, .png, .jpg, etc.; returns its `doc` handle (an already open file returns its existing handle, `already_open: true`) |

Tools that act on a document take an optional `doc`: a handle such as
`doc-3` (returned by `krita_new_canvas` and `krita_open_file`), or the
absolute path of an open document. Without it they use the active
document (`krita_close_document` is the exception: `doc` is required).
The painting tools and `krita_get_color_at` also take `layer`:
a layer's UUID, a name path such as `Head/Eyes` (`\/` for a slash in a
name), or a unique name. Painting defaults to the active layer and must
target a paint layer.

## Timeouts

This fork started as a fix for export and save timing out on large
canvases. That fix (a matching 120 s wait in `server.py` and the plugin)
is now replaced by per-command timeouts
([decisions.md D9](docs/f001_buildout/decisions.md)):

- Each plugin command has a default timeout: 30 s for queries, 300 s for
  file I/O (`get_canvas`, `save`, `open_file`, `new_canvas`, `clear`).
- Every tool except `krita_health` takes an optional `timeout_s` (up to
  900 s) to override it.
- `server.py` reads the defaults from the plugin's `/info` and waits 15 s
  longer than the plugin, so a slow command comes back as a `timeout`
  error from the plugin instead of a dropped connection.
- A command that times out before it starts is cancelled. One that is
  already running finishes in Krita (and may still change the document),
  but its result is discarded.
- `krita_health` answers while a command runs and reports it as `busy`.

## Configuration

| Setting | Default | How to Change |
|---------|---------|---------------|
| Plugin HTTP port | `5678` | Edit `SERVER_PORT` in plugin `config.py` |
| MCP server URL | `http://localhost:5678` | Set `KRITA_URL` env var |
| Canvas output dir | `~/krita-mcp-output` | Edit `CANVAS_OUTPUT_DIR` in plugin `config.py` |
| Dirs that file tools may use | output dir only | `allowed_dirs` in `~/.krita-mcp-config.json`, or `KRITA_MCP_ALLOWED_DIRS` (see below) |
| Auth token file | `~/.krita-mcp-token` | Plugin: `TOKEN_FILE` in `config.py`; MCP server: `KRITA_MCP_TOKEN_FILE` env var (or pass the token itself in `KRITA_MCP_TOKEN`) |
| Canvas size limits | 16384 px per side, 67108864 px (67.1 MP) total | `limits` in `~/.krita-mcp-config.json` |
| Request size limit | 5 MB | `MAX_BODY_BYTES` in plugin `config.py` |

### Plugin config file

The plugin reads `~/.krita-mcp-config.json` (next to the token file).
Every key is optional:

```json
{
  "allowed_dirs": ["~/umamo-art", "~/projects/mao"],
  "limits": {"max_canvas_dim": 16384, "max_canvas_pixels": 67108864},
  "journal": {"max_actions": 20, "max_bytes": 1073741824}
}
```

- **Permissions.** The file must be owned by you and not group- or
  world-writable (`chmod 600 ~/.krita-mcp-config.json`); otherwise it is
  ignored as a whole and `krita_health` says why.
- **No restart needed.** The plugin re-reads the file whenever it changes
  (mtime, size, mode), and notices a listed directory being created later.
- **Allowed dirs.** File tools (`save`, `open_file`, …) may only touch
  paths inside the output dir or one of these. That includes saving a
  document in place: its own file must be inside them too. `save`
  creates missing folders inside them. Each entry must be an
  absolute (or `~`) path to an existing directory; symlinks are resolved.
  `/`, your home directory and its parents are refused as too broad.
  Relative tool paths still resolve against the output dir.
- **Limits.** `max_canvas_dim` (per side) and `max_canvas_pixels` (total)
  bound `new_canvas`; over either gives a `too_large` error. Each value
  must be a positive integer, or its default is kept.
- **Scripted launches.** `KRITA_MCP_ALLOWED_DIRS` (`:`-separated) in
  **Krita's** environment adds more dirs. macOS apps started from the Dock
  or Finder don't see shell variables, so the file is the usual way.
  `KRITA_MCP_CONFIG_FILE` (also Krita's environment) reads a different
  config file instead of `~/.krita-mcp-config.json`.
- **Check it.** `krita_health` returns `allowed_dirs` (the output dir
  first), `limits`, `config_file` and `config_errors` (bad keys or values,
  skipped dirs, a refused file); its summary lists the errors.

Only the plugin reads this file. The MCP server can't widen the allowed
dirs.

## Security

The plugin's HTTP server only listens on `127.0.0.1`, and:

- **Requires a shared token.** On first start the plugin writes a random token to `~/.krita-mcp-token` (readable only by you). The MCP server reads it and sends it in the `X-Krita-MCP-Token` header. Requests without it are refused (except `/health`).
- **Refuses browser requests.** Requests carrying `Origin` or `Sec-Fetch-Site` headers, without `Content-Type: application/json`, or with a `Host` other than `localhost`/`127.0.0.1` are rejected. This blocks web pages from driving Krita (CSRF and DNS rebinding).
- **Confines file access.** `save` and `open_file` only accept image files inside the allowed dirs: the output dir plus those in `~/.krita-mcp-config.json` (which must be yours and not group/world-writable). Paths are resolved with `realpath` first, so `..` and symlinks can't escape. `get_canvas` always writes into the output dir. The plugin, not the MCP server, decides the allowed dirs. `/health` needs no token and lists them, so other local processes can see those directory names.
- **Caps resource use.** Canvas size, brush size, stroke point count and request size are limited so a single request can't freeze Krita.

## Painting Approach

The plugin paints using **direct pixel manipulation** (not Krita's native brush engine for strokes). This means:

- Strokes use a custom soft-circle renderer with configurable hardness
- Alpha blending is done manually in BGRA pixel format
- Shapes (rectangle, ellipse, line) are rasterized directly
- This approach is reliable and doesn't depend on Krita's internal brush state

The `set_brush` tool does set Krita's brush preset (for potential future use), but `stroke` currently uses its own pixel-level rendering.

## Development

Install the dev dependencies (pytest, Pillow) into the repo venv:

```bash
.venv/bin/pip install -r requirements-dev.txt
```

Install the plugin from this checkout by symlink, so edits only need a
Krita restart:

```bash
tools/install_plugin.sh          # refuses if a copied install is in the way
tools/install_plugin.sh --force  # moves a copied install aside to <name>.bak-<timestamp>
```

There are three test suites:

| Suite | Command | Needs |
|---|---|---|
| Unit (`tests/unit`) | `make test` | Nothing |
| Headless Krita (`tests/krita`) | `make test-krita` | `tools/install_test_runner.sh` once. The GUI Krita may stay open |
| Live (`tests/live`) | `make test-live` | GUI Krita running with the plugin from this checkout |

- **Headless** tests are stdlib `unittest` and run inside Krita's own
  Python via `kritarunner` (no window, separate profile).
  `tools/install_test_runner.sh` symlinks the runner shim into that
  profile. Results are written to `~/krita-mcp-output/test-krita/`
  (`report.xml`, plus `run.log` with the full Krita log). Run a subset with
  `make test-krita KRITA_TESTS=pixel`; point at another Krita with
  `KRITARUNNER=/path/to/kritarunner`.
- **Live** tests are skipped unless `KRITA_MCP_LIVE=1`, which
  `make test-live` sets. Plain `.venv/bin/pytest` runs unit tests and skips
  live ones.

Plugin and headless test code runs on Krita's Python (3.13), so keep it
3.13-compatible even though the venv may be newer.

## Further Documentation

Architecture, Krita API notes and feature buildout plans are in [`docs/`](docs/README.md).

## License

MIT
