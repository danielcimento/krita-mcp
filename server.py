"""
Krita MCP Server
Bridge between Claude (or any MCP client) and Krita painting application.

Uses FastMCP to expose Krita painting tools over the Model Context Protocol,
communicating with a Krita plugin via HTTP (krita_mcp_server/client.py).

Every tool returns a dict: the plugin's payload fields, a one-line
``summary`` and ``elapsed_ms``. Failures raise ``ToolError("<code>: <message>")``
(docs/f001_buildout/decisions.md D2).
"""

import os
import sys
import time
from typing import Optional

# server.py is the entry point client configs point at; make the
# krita_mcp_server package next to it importable from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from krita_mcp_server import client  # noqa: E402

# Configuration (KRITA_URL, KRITA_MCP_TOKEN_FILE, KRITA_MCP_TOKEN) is read in
# krita_mcp_server/client.py.
KRITA_URL = client.KRITA_URL

# Export/save can be slow on large canvases; matches the plugin's 120 s wait
# (bridge.RESULT_TIMEOUT_S). P1.5 derives this from the command's timeout.
LONG_TIMEOUT = 120.0

mcp = FastMCP("krita-mcp")


def _done(result: dict, summary: str) -> dict:
    return {"summary": summary, **result}


@mcp.tool()
def krita_health() -> dict:
    """Check if Krita is running and the MCP plugin is active.

    Never fails: when Krita can't be reached it returns ``running: false``.
    ``protocol_ok`` is false when the installed plugin doesn't match this
    server (update the plugin and restart Krita). ``allowed_dirs`` lists the
    directories file tools may use (the first is the output dir), and
    ``config_errors`` the problems with the plugin's config file
    (``~/.krita-mcp-config.json``), such as a skipped directory.
    """
    start = time.perf_counter()
    try:
        data = client.get_health(timeout=5.0)
    except ToolError as e:
        if str(e).startswith("unavailable:"):
            summary = (f"Cannot connect to Krita at {client.KRITA_URL}. "
                       "Make sure Krita is running with the MCP plugin enabled.")
        else:
            summary = f"Krita at {client.KRITA_URL} did not answer /health properly: {e}"
        return {"running": False, "error": str(e), "summary": summary}
    elapsed_ms = int(round((time.perf_counter() - start) * 1000))
    protocol_ok = data.get("protocol") == client.PROTOCOL
    if protocol_ok:
        summary = (f"Krita {data.get('krita_version') or '(version unknown)'} is running with "
                   f"plugin {data.get('plugin', 'unknown')} {data.get('version', '')}".rstrip()
                   + f" (protocol {data.get('protocol')})")
    else:
        mismatch = client.protocol_mismatch_message(data.get("protocol")).split(": ", 1)[1]
        summary = f"PROTOCOL MISMATCH: Krita is running, but tools will fail. {mismatch}"
    allowed_dirs = data.get("allowed_dirs") or []
    config_errors = data.get("config_errors") or []
    if allowed_dirs:
        summary += f"; {len(allowed_dirs)} allowed dir{'s' if len(allowed_dirs) != 1 else ''}"
    if config_errors:
        listed = "; ".join(" ".join(str(e).split()) for e in config_errors)  # one line
        summary += (f"; {len(config_errors)} config error{'s' if len(config_errors) != 1 else ''}: "
                    + listed)
    return {**data, "allowed_dirs": allowed_dirs, "config_errors": config_errors,
            "running": True, "protocol_ok": protocol_ok,
            "elapsed_ms": elapsed_ms, "summary": summary}


@mcp.tool()
def krita_new_canvas(
    width: int = 800,
    height: int = 600,
    name: str = "New Canvas",
    background: str = "#1a1a2e"
) -> dict:
    """
    Create a new canvas in Krita. Limits: 16384 px per side and 67108864
    pixels in total by default, configurable by the user in the plugin's
    ~/.krita-mcp-config.json ("limits"); over a limit fails with too_large.

    Args:
        width: Canvas width in pixels (default 800)
        height: Canvas height in pixels (default 600)
        name: Document name
        background: Background color as hex (default dark blue)
    """
    result = client.call("new_canvas", {
        "width": width,
        "height": height,
        "name": name,
        "background": background
    })
    return _done(result, f"Created canvas '{result.get('name', name)}': "
                         f"{result.get('width', width)}x{result.get('height', height)}, "
                         f"background {background}")


@mcp.tool()
def krita_set_color(color: str) -> dict:
    """
    Set the foreground (paint) color.

    Args:
        color: Hex color code (e.g., "#ff6b6b", "#b8a9c9")
    """
    result = client.call("set_color", {"color": color})
    return _done(result, f"Color set to {result.get('color', color)}")


@mcp.tool()
def krita_set_brush(
    preset: Optional[str] = None,
    size: Optional[int] = None,
    opacity: Optional[float] = None
) -> dict:
    """
    Set brush preset and properties.

    Args:
        preset: Brush preset name (partial match, e.g., "Basic", "Soft", "Airbrush")
        size: Brush size in pixels (1 to 1000)
        opacity: Brush opacity (0.0 to 1.0)
    """
    params = {}
    if preset:
        params["preset"] = preset
    if size is not None:
        params["size"] = size
    if opacity is not None:
        params["opacity"] = opacity

    result = client.call("set_brush", params)
    changed = [f"{k}={result.get(k, v)}" for k, v in params.items()]
    return _done(result, "Brush set: " + ", ".join(changed) if changed else "Brush unchanged")


@mcp.tool()
def krita_stroke(points: list[list[int]], pressure: float = 1.0) -> dict:
    """
    Paint a stroke through a series of points.

    Args:
        points: List of [x, y] coordinate pairs, e.g., [[100, 100], [150, 120], [200, 150]]
        pressure: Brush pressure (0.0 to 1.0, affects stroke thickness/opacity)
    """
    if len(points) < 2:
        raise ToolError("bad_request: Need at least 2 points for a stroke")

    result = client.call("stroke", {
        "points": points,
        "pressure": pressure
    })
    return _done(result, f"Stroke painted with {result.get('points_count', len(points))} points")


@mcp.tool()
def krita_fill(x: int, y: int, radius: int = 50) -> dict:
    """
    Fill an area with current color (paints a filled circle at the point).

    Args:
        x: X coordinate
        y: Y coordinate
        radius: Fill radius in pixels
    """
    result = client.call("fill", {"x": x, "y": y, "radius": radius})
    return _done(result, f"Filled circle at ({x}, {y}) with radius {radius}")


@mcp.tool()
def krita_draw_shape(
    shape: str,
    x: int,
    y: int,
    width: int = 100,
    height: int = 100,
    fill: bool = True,
    stroke: bool = False,
    x2: Optional[int] = None,
    y2: Optional[int] = None
) -> dict:
    """
    Draw a shape on the canvas.

    Args:
        shape: Type of shape - "rectangle", "ellipse", or "line"
        x: X coordinate (top-left for shapes, start point for lines)
        y: Y coordinate (top-left for shapes, start point for lines)
        width: Width of shape (ignored for lines if x2/y2 provided)
        height: Height of shape (ignored for lines if x2/y2 provided)
        fill: Whether to fill the shape
        stroke: Whether to draw outline
        x2: End X for lines (optional)
        y2: End Y for lines (optional)
    """
    params = {
        "shape": shape,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "fill": fill,
        "stroke": stroke
    }
    if x2 is not None:
        params["x2"] = x2
    if y2 is not None:
        params["y2"] = y2

    result = client.call("draw_shape", params)
    return _done(result, f"Drew {result.get('shape', shape)} at ({x}, {y})")


@mcp.tool()
def krita_get_canvas(filename: str = "canvas.png") -> dict:
    """
    Export current canvas to a PNG file and return the path.
    Use this to see your painting progress.

    Args:
        filename: Output filename (saved to configured output directory)
    """
    result = client.call("get_canvas", {"filename": filename}, timeout=LONG_TIMEOUT)
    return _done(result, f"Canvas saved to: {result.get('path', '')}")


@mcp.tool()
def krita_undo() -> dict:
    """Undo the last action."""
    result = client.call("undo", {})
    return _done(result, "Undone")


@mcp.tool()
def krita_redo() -> dict:
    """Redo the last undone action."""
    result = client.call("redo", {})
    return _done(result, "Redone")


@mcp.tool()
def krita_clear(color: str = "#1a1a2e") -> dict:
    """
    Clear the canvas to a solid color.

    Args:
        color: Color to fill canvas with (default dark blue)
    """
    result = client.call("clear", {"color": color})
    return _done(result, f"Canvas cleared to {result.get('color', color)}")


@mcp.tool()
def krita_save(path: str) -> dict:
    """
    Save the current canvas to a file inside the plugin's allowed
    directories (by default ~/krita-mcp-output).

    Args:
        path: File name or path; relative paths are resolved against the
              output directory (e.g., "my_painting.png")
    """
    result = client.call("save", {"path": path}, timeout=LONG_TIMEOUT)
    return _done(result, f"Saved to {result.get('path', path)}")


@mcp.tool()
def krita_get_color_at(x: int, y: int) -> dict:
    """
    Sample the color at a specific pixel (eyedropper).

    Args:
        x: X coordinate
        y: Y coordinate
    """
    result = client.call("get_color_at", {"x": x, "y": y})
    return _done(result, f"Color at ({x}, {y}): {result.get('color', 'unknown')} "
                         f"(R:{result.get('r')}, G:{result.get('g')}, B:{result.get('b')}, "
                         f"A:{result.get('a')})")


@mcp.tool()
def krita_list_brushes(filter: str = "", limit: int = 20) -> dict:
    """
    List available brush presets.

    Args:
        filter: Filter brushes by name (partial match)
        limit: Maximum number to return
    """
    result = client.call("list_brushes", {"filter": filter, "limit": limit})
    brushes = result.get("brushes", [])
    if not brushes:
        summary = "No brushes found matching filter" + (f" '{filter}'" if filter else "")
    else:
        summary = f"Found {len(brushes)} brushes" + (f" matching '{filter}'" if filter else "")
    return _done(result, summary)


@mcp.tool()
def krita_open_file(path: str) -> dict:
    """
    Open an existing image file in Krita (.kra, .png, .jpg, etc). Only files
    inside the plugin's allowed directories (by default ~/krita-mcp-output)
    can be opened.

    Args:
        path: File name or path; relative paths are resolved against the
              output directory (e.g., "my_painting.kra")
    """
    result = client.call("open_file", {"path": path}, timeout=30.0)
    return _done(result, f"Opened {result.get('name') or result.get('path', path)} "
                         f"({result.get('width')}x{result.get('height')})")


if __name__ == "__main__":
    mcp.run()
