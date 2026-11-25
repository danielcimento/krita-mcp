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
| `krita_new_canvas` | Create a new canvas (width, height, background color) |
| `krita_set_color` | Set foreground paint color (hex) |
| `krita_set_brush` | Set brush preset, size, and opacity |
| `krita_stroke` | Paint a stroke through a list of [x, y] points |
| `krita_fill` | Fill a circular area at a point |
| `krita_draw_shape` | Draw rectangle, ellipse, or line |
| `krita_get_canvas` | Export canvas to PNG (for AI to see progress) |
| `krita_save` | Save canvas to a specific file path |
| `krita_undo` / `krita_redo` | Undo/redo actions |
| `krita_clear` | Clear canvas to a solid color |
| `krita_get_color_at` | Eyedropper — sample color at a pixel |
| `krita_list_brushes` | List available brush presets |
| `krita_open_file` | Open an existing .kra, .png, .jpg, etc. |

## The Export Timeout Fix

**This is the main reason this repo exists.**

By default, HTTP requests and command queue operations time out after ~30 seconds. Canvas export (`get_canvas`) and file save (`save`) operations can easily exceed this on larger canvases, causing silent failures or timeout errors.

The fix is applied in two places:

**MCP Server (`server.py`)** — Extended timeout for export/save commands:
```python
# In krita_get_canvas and krita_save:
result = send_command("get_canvas", {"filename": filename}, timeout=120.0)
result = send_command("save", {"path": path}, timeout=120.0)
```

**Krita Plugin (`__init__.py`)** — Matching timeout in the command queue:
```python
def get_result(self, command_id, timeout=120):
    """Wait for result with timeout."""
    for _ in range(int(timeout * 10)):  # Check every 100ms
        ...
```

**Both sides must match.** If only the MCP server timeout is increased, the plugin's command queue will still time out at 30s. If only the plugin timeout is increased, the HTTP request from the MCP server will time out first.

## Configuration

| Setting | Default | How to Change |
|---------|---------|---------------|
| Plugin HTTP port | `5678` | Edit `SERVER_PORT` in plugin `__init__.py` |
| MCP server URL | `http://localhost:5678` | Set `KRITA_URL` env var |
| Canvas output dir | `~/krita-mcp-output` | Edit `CANVAS_OUTPUT_DIR` in plugin `__init__.py` |
| Dirs that save/open may use | output dir only | Edit `ALLOWED_DIRS` in plugin `__init__.py` |
| Auth token file | `~/.krita-mcp-token` | Plugin: `TOKEN_FILE`; MCP server: `KRITA_MCP_TOKEN_FILE` env var (or pass the token itself in `KRITA_MCP_TOKEN`) |
| Size limits | 8192 px per side, 16 MP total, 5 MB requests | `MAX_*` constants in plugin `__init__.py` |

## Security

The plugin's HTTP server only listens on `127.0.0.1`, and:

- **Requires a shared token.** On first start the plugin writes a random token to `~/.krita-mcp-token` (readable only by you). The MCP server reads it and sends it in the `X-Krita-MCP-Token` header. Requests without it are refused (except `/health`).
- **Refuses browser requests.** Requests carrying `Origin` or `Sec-Fetch-Site` headers, without `Content-Type: application/json`, or with a `Host` other than `localhost`/`127.0.0.1` are rejected. This blocks web pages from driving Krita (CSRF and DNS rebinding).
- **Confines file access.** `save` and `open_file` only accept image files inside `ALLOWED_DIRS`; `get_canvas` always writes into the output dir.
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
