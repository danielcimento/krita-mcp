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

**Documents**

| Tool | Description |
|------|-------------|
| `krita_health` | Whether Krita and the plugin are running; protocol, allowed dirs, limits, lint settings, config errors, the busy command |
| `krita_list_documents` | Open documents with their `doc` handles, size, path, modified and active flags |
| `krita_get_document_info` | One document's size, resolution, colour space, layer counts, nesting depth and selection |
| `krita_new_canvas` | New RGBA canvas (size, background colour or `"transparent"`, profile, resolution); returns its `doc` handle |
| `krita_open_file` | Open a `.kra`, `.ora`, `.png`, …; returns its `doc` handle (an already open file keeps its handle) |
| `krita_save` | Save in place, save as `.kra` (the document moves there), or export to PNG/JPG/PSD/ORA; Add / Linear Dodge layers are converted for `.psd` / `.ora` on a copy (`blend_conversions`) |
| `krita_close_document` | Close a document by handle; refuses unsaved changes unless `discard=true` |

**Layer tree**

| Tool | Description |
|------|-------------|
| `krita_list_layers` | The tree as a flat list, top to bottom: id, name, path, type, parent, visibility, opacity, blend mode, locks, exact bounds; `root`, `depth`, `fields` |
| `krita_create_layers` | Groups and paint layers in one all-or-nothing call, from specs (`key` / `"$key"` nesting, properties, `png` pixels) or a `manifest.json`; `dry_run` |
| `krita_update_layers` | Change name, visibility, opacity, blend mode, locks, clipping, pass-through of many layers at once; `dry_run` |
| `krita_move_layers` | Reorder and reparent layers (`parent`, `above` / `below` / `index`); groups move with their subtree; `dry_run` |
| `krita_delete_layers` | Delete layers (a group with its subtree) in one call; `dry_run` |
| `krita_duplicate_layer` | Copy a layer (a group with its subtree, a layer with its masks; new ids) directly above it, or under `parent` / above `above`; `name`, `dry_run` |
| `krita_set_active_layer` | Select a layer in the Layers docker (what painting without `layer` paints on); UI state, not an undo step |

**Pixels**

| Tool | Description |
|------|-------------|
| `krita_import_png` | Write a PNG into a paint layer at `x`/`y`: `replace` (exact) or `over`, optional `clear_outside`; one undo step |
| `krita_trim_layers` | Clear faint pixels (alpha below `alpha_threshold`, colour zeroed too) in paint layers, optionally only those farther than `near_px` from stronger pixels (keeps anti-aliased edges); `dry_run`, one undo step |
| `krita_export_layers` | Each paint layer's own pixels as a PNG (cropped to alpha > 0) plus a `manifest.json` that `krita_create_layers` rebuilds the tree from |
| `krita_render` | Render to a PNG without changing the document: `region`, `scale` (0.25 for fast previews), `only` / `hide` layers, `background`, `png_level` |
| `krita_get_canvas` | The whole canvas as a PNG in the output dir; a shortcut for a full-canvas `krita_render` |
| `krita_get_color_at` | Eyedropper: the colour at a pixel, of the composite or of one layer |

**Selections**

| Tool | Description |
|------|-------------|
| `krita_select` | Set the selection: `rect`, `polygon` (antialiased or not), `alpha_of` a layer, `invert`, `none`, `all`; `op` replace / add / subtract / intersect; then `grow`, `shrink`, `feather`. Not an agent undo step |
| `krita_selection_to_layer` | Cut (or copy) a paint layer's pixels inside the selection to a new layer at the same position, alpha scaled by the selection (soft edges from feathering); never touches the clipboard; `dry_run`, one undo step |

**Analysis**

| Tool | Description |
|------|-------------|
| `krita_lint` | Check a document against the Live2D rig rules (node types, layer styles, clipping, blend modes, duplicate names, side tokens, empty / off-canvas layers, stray-alpha specks, colour space); opt-in `"conventions"` set (nesting depth, name case, blend suffixes, pose visibility, hidden references, mask suffixes); `rules`, `severity_min`; findings sorted by stacking order |
| `krita_layer_stats` | Per paint layer: bounds, opaque bounds, pixel and opaque counts, an alpha histogram (`buckets`), empty, overhang past the canvas, lint's stray-alpha numbers |

**Painting**

| Tool | Description |
|------|-------------|
| `krita_set_color` | Set the paint colour (kept by the plugin, so it works without a window) |
| `krita_set_brush` | Set the brush preset, size and opacity |
| `krita_list_brushes` | List brush presets |
| `krita_stroke` | Paint a stroke through a list of [x, y] points |
| `krita_fill` | Fill a circle at a point |
| `krita_draw_shape` | Draw a rectangle, ellipse or line |
| `krita_clear` | Clear a layer to a colour, or `"transparent"` |

**History**

| Tool | Description |
|------|-------------|
| `krita_undo` / `krita_redo` | Undo / redo one agent tool call on a document, from the plugin's agent journal (exact: ids, properties, order, pixels) |
| `krita_journal` | List a document's agent undo and redo stacks |
| `krita_ui_undo` / `krita_ui_redo` | Krita's own Edit → Undo / Redo on the document's view (only partly covers agent edits) |

**Two kinds of undo.** `krita_undo` / `krita_redo` use the plugin's agent
journal: one call reverts (or re-applies) one whole agent tool call, a
`create_layers` batch, an update, a move, a delete, a duplicate, a trim,
an `import_png`, a stroke or a clear, with the same layer ids, properties, order and pixels.
The journal keeps the last 20 calls per document (`journal` in the config
file), holds pixel snapshots in memory up to `max_bytes` and in temporary
files beyond that, and is dropped when the document closes. It only knows
agent edits: undoing an agent action after painting by hand on the same
layer overwrites those strokes in that rectangle, and an entry whose
layers were deleted by hand is refused. Krita's own undo stack
(`krita_ui_undo`, Edit → Undo) only partly covers agent edits: property
changes, pixel writes and moves made by agent tools aren't on it, so it may
skip them or revert an unrelated earlier step. Applying a journal entry
uses the same Krita calls, so it leaves partial steps on Krita's stack too.

Tools that act on a document take an optional `doc`: a handle such as
`doc-3` (returned by `krita_new_canvas` and `krita_open_file`), or the
absolute path of an open document. Without it they use the active
document (`krita_close_document` is the exception: `doc` is required).
The painting tools and `krita_get_color_at` also take `layer`:
a layer's UUID, a name path such as `Head/Eyes` (`\/` for a slash in a
name), or a unique name. Painting defaults to the active layer and must
target a paint layer.

### Live2D workflow

Open the artwork (`krita_open_file`) or start a canvas (`krita_new_canvas`),
build or restructure the part layers in one call with
`krita_create_layers(manifest=…)` (and the update / move / delete tools),
check the result with `krita_lint` until it reports no errors, then export
the parts with `krita_export_layers` (PNGs plus a manifest) or preview them
with `krita_render`. `tools/ora_to_manifest.py` turns an `.ora` into a
manifest for the first step.

## Timeouts

This fork started as a fix for export and save timing out on large
canvases. That fix (a matching 120 s wait in `server.py` and the plugin)
is now replaced by per-command timeouts
([decisions.md D9](docs/f001_buildout/decisions.md)):

- Each plugin command has a default timeout: 30 s for queries, 300 s for
  file I/O and long reads (`get_canvas`, `render`, `save`, `open_file`,
  `new_canvas`, `clear`, `export_layers`, `lint`, …).
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
  "journal": {"max_actions": 20, "max_bytes": 1073741824},
  "lint": {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4,
           "stray_speck_max_px": 64}
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
- **Journal.** The agent undo journal (`krita_undo`) per document:
  `max_actions` entries (default 20) on each of the undo and redo stacks,
  `max_bytes` of memory (default 1 GB) and `max_disk_bytes` (default 4 GB)
  of temporary files for the pixel snapshots that don't fit in memory
  (zlib-compressed, in a `kmcp-journal-*` dir under the system temp dir,
  removed when Krita quits). Both byte caps cover the undo and redo stacks
  together. Deleted layers are kept in memory only. A call too big for the
  caps still happens but warns `not_undoable`.
- **Lint.** `krita_lint`'s settings: `additive_mode` (`"either"`, or
  `"add"` / `"linear_dodge"` to require one spelling of the Add blend
  mode), and the stray-alpha thresholds: pixels with alpha below
  `stray_alpha_below` (1–255) further than `stray_alpha_distance` px
  (1–64) from any stronger pixel are stray, and 8-connected groups of at
  most `stray_speck_max_px` (1–1,000,000, default 64) stray pixels are
  reported as specks; larger groups (soft glows) are not.
- **Scripted launches.** `KRITA_MCP_ALLOWED_DIRS` (`:`-separated) in
  **Krita's** environment adds more dirs. macOS apps started from the Dock
  or Finder don't see shell variables, so the file is the usual way.
  `KRITA_MCP_CONFIG_FILE` (also Krita's environment) reads a different
  config file instead of `~/.krita-mcp-config.json`.
- **Check it.** `krita_health` returns `allowed_dirs` (the output dir
  first), `limits`, `lint`, `config_file` and `config_errors` (bad keys or values,
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
