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
from typing import Annotated, Literal, Optional, Union

# server.py is the entry point client configs point at; make the
# krita_mcp_server package next to it importable from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from pydantic import Field, ValidationError  # noqa: E402

from krita_mcp_server import client  # noqa: E402
from krita_mcp_server.models import LayerChange, LayerSpec, Move, Rect  # noqa: E402

# Configuration (KRITA_URL, KRITA_MCP_TOKEN_FILE, KRITA_MCP_TOKEN) is read in
# krita_mcp_server/client.py.
KRITA_URL = client.KRITA_URL

# Timeouts (D9): each command has a default in the plugin's registry (30 s
# for queries, 300 s for file I/O); every tool but krita_health takes an
# optional timeout_s (up to 900) to override it. client.call sets the HTTP
# timeout to that plus 15 s.

mcp = FastMCP("krita-mcp")


def _done(result: dict, summary: str) -> dict:
    return {"summary": summary, **result}


def _with(params: dict, **optional) -> dict:
    """``params`` plus each optional argument that is not None."""
    return {**params, **{k: v for k, v in optional.items() if v is not None}}


def _in(result: dict) -> str:
    """" in doc-3" when the plugin reported the document's handle."""
    return f" in {result['doc']}" if result.get("doc") else ""


@mcp.tool()
def krita_health() -> dict:
    """Check if Krita is running and the MCP plugin is active.

    Never fails: when Krita can't be reached it returns ``running: false``.
    ``protocol_ok`` is false when the installed plugin doesn't match this
    server (update the plugin and restart Krita). ``allowed_dirs`` lists the
    directories file tools may use (the first is the output dir).
    To allow another folder, add it to the list under ``config_key``
    ("allowed_dirs") in the JSON file ``config_file``
    (``~/.krita-mcp-config.json``; ``config_exists`` says whether it
    exists yet), e.g. {"allowed_dirs": ["~/my-project/out"]}: the folder
    must exist and must not be your home folder or above it. Changes apply
    from the next call without restarting Krita (``config_reload``);
    krita_reload_config shows the result. ``config_dirs`` are the file's
    entries as written, ``config_errors`` the problems, such as a skipped
    directory; ``limits``, ``lint`` and ``live2d`` are the canvas limits,
    krita_lint's settings and the Live2D conventions (``live2d.enabled``:
    off unless the config turns them on). ``busy``
    is the command Krita is running now (``{"action", "elapsed_ms"}``) or
    null, and ``queued`` how many wait behind it.
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
        summary += (f"; {len(allowed_dirs)} allowed dir{'s' if len(allowed_dirs) != 1 else ''}: "
                    f"{', '.join(str(d) for d in allowed_dirs)} (add more under "
                    f"\"{data.get('config_key') or 'allowed_dirs'}\" in "
                    f"{data.get('config_file') or '~/.krita-mcp-config.json'})")
    busy = data.get("busy")
    if isinstance(busy, dict):
        elapsed = busy.get("elapsed_ms")
        took = f" for {elapsed / 1000:.1f} s" if isinstance(elapsed, (int, float)) else ""
        summary += f"; busy: {busy.get('action')}{took}"
        if data.get("queued"):
            summary += f", {data['queued']} queued"
    if config_errors:
        listed = "; ".join(" ".join(str(e).split()) for e in config_errors)  # one line
        summary += (f"; {len(config_errors)} config error{'s' if len(config_errors) != 1 else ''}: "
                    + listed)
    return {**data, "allowed_dirs": allowed_dirs, "config_errors": config_errors,
            "busy": busy if isinstance(busy, dict) else None,
            "running": True, "protocol_ok": protocol_ok,
            "elapsed_ms": elapsed_ms, "summary": summary}


@mcp.tool()
def krita_reload_config(timeout_s: Optional[float] = None) -> dict:
    """
    Re-read the plugin's config file (~/.krita-mcp-config.json) now and
    report the result: allowed_dirs (resolved; the first is the output
    dir), config_file, config_exists, config_key ("allowed_dirs"),
    config_dirs (the file's entries as written), limits, lint and
    config_errors (e.g. a folder skipped because it doesn't exist). The
    plugin also re-reads the file by itself when it changes, so this is
    for checking that an edit took effect; Krita needs no restart.

    Args:
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("reload_config", {}, timeout_s=timeout_s)
    dirs = result.get("allowed_dirs") or []
    summary = f"Config reloaded from {result.get('config_file')}"
    if not result.get("config_exists"):
        summary += " (the file does not exist; defaults in use)"
    summary += f"; allowed dirs: {', '.join(str(d) for d in dirs) or 'none'}"
    errors = result.get("config_errors") or []
    if errors:
        summary += f"; {_count(len(errors), 'config error')}: " + "; ".join(
            " ".join(str(e).split()) for e in errors)
    return _done(result, summary)


# --- Documents (api-design § Documents) ------------------------------------

def _size(n) -> str:
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return "size unknown"
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _layers(counts) -> str:
    total = sum(v for v in (counts or {}).values() if isinstance(v, int))
    return f"{total} layer{'s' if total != 1 else ''}"


@mcp.tool()
def krita_list_documents(timeout_s: Optional[float] = None) -> dict:
    """
    List every open document with its handle (``doc``, e.g. "doc-3"), which
    other tools take as ``doc``. Each entry: doc, name (the file name when
    the document has no title), path (null if never saved), width and
    height in pixels, modified, and active (the document of Krita's active
    window; false for all when Krita runs without a window).

    Args:
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("list_documents", {}, timeout_s=timeout_s)
    docs = result.get("documents") or []
    shown = [f"{d.get('doc')} '{d.get('name')}'" + (" (active)" if d.get("active") else "")
             for d in docs[:10]]
    more = f", and {len(docs) - 10} more" if len(docs) > 10 else ""
    summary = (f"{len(docs)} open document{'s' if len(docs) != 1 else ''}"
               + (": " + ", ".join(shown) + more if docs else ""))
    return _done(result, summary)


@mcp.tool()
def krita_get_document_info(doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Describe one document: name, path (null if never saved), width and
    height in pixels, resolution (dpi, a whole number), color_model,
    color_depth, color_profile, modified, layer_counts (nodes per type,
    e.g. {"paintlayer": 184, "grouplayer": 38}; the global selection is not
    counted), max_depth (nesting depth: top-level layers are depth 1, an
    empty document 0) and selection (null, or the bounding rect
    {x, y, w, h} of the active selection).

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("get_document_info", _with({}, doc=doc), timeout_s=timeout_s)
    summary = (f"{result.get('doc', doc or 'active document')} '{result.get('name')}': "
               f"{result.get('width')}x{result.get('height')}, "
               f"{result.get('color_model')}/{result.get('color_depth')} "
               f"{result.get('color_profile')}, {_layers(result.get('layer_counts'))}, "
               f"depth {result.get('max_depth')}"
               + (", modified" if result.get("modified") else "")
               + (", has a selection" if result.get("selection") else ""))
    return _done(result, summary)


@mcp.tool()
def krita_new_canvas(
    width: int = 800,
    height: int = 600,
    name: str = "New Canvas",
    background: str = "#1a1a2e",
    color_profile: Optional[str] = None,
    resolution: Optional[float] = None,
    timeout_s: Optional[float] = None,
) -> dict:
    """
    Create a new RGBA 8-bit canvas in Krita and return its document handle
    (``doc``, e.g. "doc-3"), which other tools take as ``doc``. Limits:
    16384 px per side and 67108864 pixels in total by default,
    configurable by the user in the plugin's ~/.krita-mcp-config.json
    ("limits"); over a limit fails with too_large. The canvas has one
    paint layer, "Background", filled with ``background``
    (``background_layer`` is its id), or no layer at all with
    background="none" (``background_layer`` null), for a rebuild. The
    defaults are kept from earlier versions for compatibility; to rebuild
    an exported document with its own size and resolution, use
    krita_create_layers(manifest=..., new_document=true) instead.

    Args:
        width: Canvas width in pixels (default 800)
        height: Canvas height in pixels (default 600)
        name: Document name (default "New Canvas")
        background: Background color as hex (default "#1a1a2e", dark blue),
                    "transparent" for an empty Background layer, or "none"
                    for no layer
        color_profile: An installed RGBA/U8 profile name; default Krita's
                       sRGB ("sRGB-elle-V2-srgbtrc.icc"). An unknown name
                       fails with bad_request listing the available ones
        resolution: Resolution in dpi, 1 to 10000 (default 120; Krita's own
                    default and most source files are 72)
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("new_canvas", _with({
        "width": width,
        "height": height,
        "name": name,
        "background": background,
    }, color_profile=color_profile, resolution=resolution), timeout_s=timeout_s)
    handle = f" ({result['doc']})" if result.get("doc") else ""
    extra = f", {result['color_profile']}" if result.get("color_profile") else ""
    if result.get("resolution") is not None:
        extra += f", {result['resolution']} dpi"
    return _done(result, f"Created canvas '{result.get('name', name)}'{handle}: "
                         f"{result.get('width', width)}x{result.get('height', height)}, "
                         f"background {result.get('background', background)}{extra}")


@mcp.tool()
def krita_open_file(path: str, timeout_s: Optional[float] = None) -> dict:
    """
    Open an image file in Krita (.kra, .ora, .psd, .png, .jpg, etc.) and
    return its document handle (``doc``, e.g. "doc-3"), which other tools
    take as ``doc``. Only files inside the plugin's allowed directories (by
    default ~/krita-mcp-output) can be opened. If the file is already open,
    that document's handle comes back with ``already_open: true`` and the
    file isn't opened again. Also returns path (resolved), name (the file
    name when the document has no title), width and height in pixels, and
    layer_counts.

    Args:
        path: File name or path; relative paths are resolved against the
              output directory (e.g., "my_painting.kra")
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("open_file", {"path": path}, timeout_s=timeout_s)
    what = result.get("name") or result.get("path", path)
    size = (f"{result.get('width')}x{result.get('height')}, "
            f"{_layers(result.get('layer_counts'))}")
    if result.get("already_open"):
        summary = f"{what} is already open as {result.get('doc')} ({size})"
    else:
        handle = f" as {result['doc']}" if result.get("doc") else ""
        summary = f"Opened {what}{handle} ({size})"
    return _done(result, summary)


@mcp.tool()
def krita_save(path: Optional[str] = None, doc: Optional[str] = None,
               format: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Save a document. Without ``path``: save it in place, to its own file
    (conflict if it has never been saved). With ``path`` (inside the
    plugin's allowed directories, by default ~/krita-mcp-output; missing
    folders are created): a .kra file is a "save as", so the document now
    lives there; any other format is an export, and the document's own
    path is unchanged. Existing files are overwritten. Returns path
    (resolved), format, bytes (file size), doc_path_changed and
    blend_conversions.

    Add and Linear Dodge render identically, but PSD loses Add (it is
    written as Normal) and only Krita reads ORA's Linear Dodge. So a .psd
    save writes "add" layers as "linear_dodge", and a .ora save writes
    "linear_dodge" layers as "add", on a copy: the open document keeps its
    modes. blend_conversions lists them ({id, path, from, to}); empty
    otherwise. .kra saves are never converted.

    Args:
        path: File name or path; relative paths are resolved against the
              output directory (e.g., "my_painting.png"). Omit to save in place
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        format: File format such as "kra", "png", "jpg"; default the
                extension of ``path``. Given with a path whose extension
                differs, fails with bad_request
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("save", _with({}, path=path, doc=doc, format=format),
                         timeout_s=timeout_s)
    who = result.get("doc") or "document"
    where = result.get("path", path or "its file")
    info = f" ({result.get('format', format or '?')}, {_size(result.get('bytes'))})"
    if path is None:
        summary = f"Saved {who} in place to {where}{info}"
    elif result.get("format") == "kra" or result.get("doc_path_changed"):
        moved = "; the document now lives there" if result.get("doc_path_changed") else ""
        summary = f"Saved {who} to {where}{info}{moved}"
    else:
        summary = f"Exported {who} to {where}{info}"
    return _done(result, summary + _blend_conversions(result))


# Krita blend-mode id -> its name in Krita's UI, for the save summary (D17).
_MODE_WORDS = {"add": "Add", "linear_dodge": "Linear Dodge"}


def _blend_conversions(result: dict) -> str:
    """"; 2 Add layers written as Linear Dodge for PSD" for a save that
    converted additive blend modes (D17), else ""."""
    pairs: dict = {}
    for c in result.get("blend_conversions") or []:
        key = (c.get("from"), c.get("to"))
        pairs[key] = pairs.get(key, 0) + 1
    fmt = str(result.get("format", "")).upper()
    parts = []
    for (src, dst), n in pairs.items():
        layers = "layer" if n == 1 else "layers"
        parts.append(f"{n} {_MODE_WORDS.get(src, src)} {layers} written as "
                     f"{_MODE_WORDS.get(dst, dst)} for {fmt}")
    return "".join(f"; {p}" for p in parts)


@mcp.tool()
def krita_close_document(doc: str, discard: bool = False,
                         timeout_s: Optional[float] = None) -> dict:
    """
    Close a document. ``doc`` is required (there is no active-document
    default, so the wrong document can't be closed by accident). A
    document with unsaved changes fails with conflict unless ``discard``
    is true, which drops the changes. The handle is retired: later calls
    with it fail with not_found.

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document
        discard: Close even with unsaved changes, losing them (default false)
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("close_document", {"doc": doc, "discard": discard},
                         timeout_s=timeout_s)
    return _done(result, f"Closed {result.get('doc', doc)}"
                         + (" (unsaved changes, if any, discarded)" if discard else ""))


CanvasAnchor = Literal["top-left", "top", "top-right", "left", "center", "right", "bottom-left",
                       "bottom", "bottom-right"]


@mcp.tool()
def krita_set_canvas_size(width: Annotated[int, Field(ge=1)], height: Annotated[int, Field(ge=1)],
                          anchor: CanvasAnchor = "center", doc: Optional[str] = None,
                          dry_run: bool = False, timeout_s: Optional[float] = None) -> dict:
    """
    Change the canvas size without scaling anything (Image > Resize Canvas):
    the art keeps its place relative to itself and is shifted by the offset
    the anchor gives. With the Live2D conventions on (config
    live2d.enabled), every result carries a "rig_breaking" warning: a
    resize breaks any rig already made on the document (mesh coordinates
    are canvas pixels), so fix the canvas size before rigging starts.

    anchor is the part of the old canvas that stays put: "top-left" keeps
    the top-left corner, "center" grows or shrinks evenly on all sides,
    "bottom" keeps the bottom edge centred, ... With "center" (or "top",
    "left", ...) and an odd difference the art sits half a pixel left of or
    above the exact centre (offset = floor(difference / 2)): growing 10 ->
    13 adds 1 column on the left and 2 on the right, shrinking 13 -> 10
    removes 2 on the left and 1 on the right. offset {x, y} in the result
    is where the old canvas's top-left corner lands on the new one (every
    layer moves by it; negative when the left / top is cropped).

    Shrinking crops nothing from the layers: pixels pushed past the new
    edge stay in them (invisible, but there for a later grow or undo).
    Masks and the selection move with the art. Width and height are
    checked against the canvas limits (too_large). One call is one agent
    undo step (krita_undo restores the size and every layer exactly).

    Returns {doc, width_before, height_before, width, height, offset {x, y},
    anchor, dry_run, changed (false when the size is already the same, then
    nothing is done), warnings}; with dry_run nothing changes and offset is
    the planned one.

    Args:
        width: New canvas width in pixels
        height: New canvas height in pixels
        anchor: top-left, top, top-right, left, center (default), right,
                bottom-left, bottom or bottom-right
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report the offset
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = _with({"width": width, "height": height, "anchor": anchor}, doc=doc,
                   dry_run=dry_run or None)
    result = client.call("set_canvas_size", params, timeout_s=timeout_s)
    where = result.get("doc") or doc or "the active document"
    before = f"{result.get('width_before')}x{result.get('height_before')}"
    after = f"{result.get('width', width)}x{result.get('height', height)}"
    off = result.get("offset") or {}
    how = f"anchor {result.get('anchor', anchor)}, content moved by ({off.get('x')}, {off.get('y')})"
    warnings = result.get("warnings") or []
    rig = any(isinstance(w, dict) and w.get("code") == "rig_breaking" for w in warnings)
    others = [w for w in warnings if
              not (isinstance(w, dict) and w.get("code") == "rig_breaking")]
    if result.get("dry_run"):
        summary = (f"Would resize the canvas of {where} from {before} to {after} ({how})"
                   + ("; this would break any existing rig" if rig else "")
                   + " (dry run, nothing changed)")
    elif result.get("changed") is False:
        summary = f"The canvas of {where} is already {after}: nothing changed"
    else:
        summary = (f"Resized the canvas of {where} from {before} to {after} ({how})"
                   + ("; this breaks any existing rig (mesh coordinates are canvas pixels)"
                      if rig else ""))
    return _done(result, summary + _warnings_note(others))


# --- Layer tree (api-design § Layer tree) ----------------------------------

# Node type -> (singular, plural) for summaries. Others print as the type.
_TYPE_WORDS = {
    "grouplayer": ("group", "groups"),
    "paintlayer": ("paint layer", "paint layers"),
    "filterlayer": ("filter layer", "filter layers"),
    "filllayer": ("fill layer", "fill layers"),
    "clonelayer": ("clone layer", "clone layers"),
    "vectorlayer": ("vector layer", "vector layers"),
    "filelayer": ("file layer", "file layers"),
    "transparencymask": ("transparency mask", "transparency masks"),
    "filtermask": ("filter mask", "filter masks"),
    "transformmask": ("transform mask", "transform masks"),
    "colorizemask": ("colorize mask", "colorize masks"),
}


def _type_counts(counts) -> str:
    """"38 groups, 184 paint layers": groups first, then paint layers, then
    the rest by type name. Empty string when nothing is counted."""
    items = [(k, v) for k, v in (counts or {}).items()
             if k != "total" and isinstance(v, int) and not isinstance(v, bool) and v > 0]
    first = {"grouplayer": 0, "paintlayer": 1}
    items.sort(key=lambda kv: (first.get(kv[0], 2), kv[0]))
    parts = []
    for kind, n in items:
        one, many = _TYPE_WORDS.get(kind, (kind, kind))
        parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts)


# list_layers' default fields and page size (F003 K3): about 190 bytes per
# layer, so a page plus the summary stays under 8 KB.
LIST_DEFAULT_FIELDS = ["id", "name", "type", "visible", "blend_mode", "children"]
LIST_DEFAULT_LIMIT = 30


def _summary_note(summary) -> str:
    """"; 2 hidden, 7 multiply, depth 4" from a list_layers overview."""
    if not isinstance(summary, dict):
        return ""
    parts = []
    hidden = (summary.get("hidden") or {}).get("count")
    if hidden:
        parts.append(f"{hidden} hidden")
    for mode, entry in (summary.get("blend_modes") or {}).items():
        if isinstance(entry, dict) and entry.get("count"):
            parts.append(f"{entry['count']} {mode}")
    if summary.get("max_depth"):
        parts.append(f"depth {summary['max_depth']}")
    return ("; " + ", ".join(parts)) if parts else ""


@mcp.tool()
def krita_list_layers(doc: Optional[str] = None, root: Optional[str] = None,
                      depth: Optional[int] = None, fields: Optional[list[str]] = None,
                      summary_only: bool = False, path: Optional[str] = None,
                      limit: Annotated[int, Field(ge=1, le=100000)] = LIST_DEFAULT_LIMIT,
                      offset: Annotated[int, Field(ge=0)] = 0,
                      timeout_s: Optional[float] = None) -> dict:
    """
    List a document's layer tree as a flat list of layer objects,
    depth-first and top to bottom (the order of Krita's Layers docker: a
    group comes right before its contents). Returns {doc, root, layers,
    counts, overview, offset, limit, next_offset}: root is "/" or the root
    layer's id; counts has nodes per type over everything under root (e.g.
    {"grouplayer": 38, "paintlayer": 184}) and "total". The document's
    global selection is never listed.

    Big trees come in pages so the result stays small: `limit` layers
    (default 30) from `offset`; next_offset is where the next page starts,
    null after the last. overview describes the whole listing: counts,
    max_depth, hidden (layers whose own visibility is off: count and up to
    20 paths) and blend_modes (each non-normal mode: count and up to 20
    paths). summary_only=true returns just counts and overview;
    path="tree.json" writes the full listing (every field, every layer) to
    a JSON file inside an allowed directory and returns the path and
    overview instead.

    Each layer object has, by default, id (braced UUID), name, type
    ("paintlayer", "grouplayer", ...), visible (the layer's own flag),
    blend_mode (a Krita id such as "normal", "multiply", "add") and
    children (child ids, top to bottom). fields chooses others: path (name
    path from the document root, "/" in names escaped as "\\/"), parent
    (id, null at the top level), index (position in the parent counted
    from the bottom), opacity (0.0-1.0), locked, alpha_locked,
    inherit_alpha (clipping), pass_through (bool on groups, null
    otherwise), has_layer_style, bounds ({x, y, w, h} in exact canvas
    pixels, null for a layer with no pixels; not clipped to the canvas);
    ["all"] gives every field.

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        root: Layer to list from: a layer UUID, a name path such as
              "Head/Eyes" ("\\/" for a slash in a name), or a unique name.
              Default "/", the whole document (the root itself isn't a
              layer and isn't listed). A layer root is listed first,
              followed by its subtree
        depth: Levels below root to list, at least 1; default unlimited.
               1 = root's direct children only (for a layer root: the layer
               and its direct children). Layers at the cut-off still list
               their children ids, so pass one as root to go deeper
        fields: Layer-object keys to include, e.g. ["path", "bounds"], or
                ["all"]; id, name and children are always included. Unknown
                keys fail with bad_request. Default id, name, type, visible,
                blend_mode, children
        summary_only: Return only counts and overview, no layers
        path: Write the full listing to this .json file (absolute, ~, or
              relative to the output dir; inside an allowed directory)
              and return its path; not with summary_only
        limit: Layers per page, default 30
        offset: Layers to skip, default 0 (pass next_offset for the next page)
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    if fields is not None and list(fields) == ["all"]:
        send_fields = None
    else:
        send_fields = LIST_DEFAULT_FIELDS if fields is None else list(fields)
    params = _with({"overview": True}, doc=doc, root=root, depth=depth, fields=send_fields)
    if summary_only:
        params["summary_only"] = True
    elif path is not None:
        params["path"] = path
    else:
        params.update(limit=limit, offset=offset)
    result = client.call("list_layers", params, timeout_s=timeout_s)
    layers = result.get("layers") or []
    counts = result.get("counts") or {}
    what = _type_counts(counts) or "No layers"
    if result.get("root") not in (None, "/") and layers:
        first = layers[0]
        what += f" under '{first.get('path') or first.get('name')}'"
    summary = what + (_in(result) or f" in {doc or 'the active document'}")
    if depth is not None:
        summary += f" (depth {depth})"
    summary += _summary_note(result.get("overview"))
    if result.get("path"):
        summary += f"; full listing written to {result['path']} ({_size(result.get('bytes'))})"
    elif not summary_only and "offset" in result:
        shown_from = result.get("offset", 0)
        summary += f"; listed {shown_from + 1 if layers else shown_from}-{shown_from + len(layers)}"
        if result.get("next_offset") is not None:
            summary += f" (next page: offset={result['next_offset']})"
    return _done(result, summary)


@mcp.tool()
def krita_create_layers(specs: Optional[list[LayerSpec]] = None, manifest: Optional[str] = None,
                        doc: Optional[str] = None, new_document: bool = False,
                        name: Optional[str] = None, dry_run: bool = False,
                        verbose: Optional[bool] = None,
                        timeout_s: Optional[float] = None) -> dict:
    """
    Create groups and paint layers in one all-or-nothing call, from either
    a list of specs or a layer manifest file (pass exactly one). Every
    layer is validated before anything changes (refs, parents, PNG
    headers, names, blend modes, unknown keys); the first bad one fails the
    call with details.index and nothing is created. If Krita fails halfway,
    the layers made so far are removed again (details.applied /
    rolled_back).

    Order: layers are created in list order and each new layer goes on top
    of its parent's children, unless "above" names a sibling to insert it
    directly above. So listing siblings bottom to top reproduces a stack.
    A spec's "key" lets later specs in the same call use "$<key>" as their
    parent or above (a "$key" defined by a later spec is not_found); layers
    made in this call are reachable only that way, not by name.

    Each spec: name (required); key; type "paint" (default) or "group";
    parent (a group ref, "$key", or "/" the root, default); above; visible
    (default true); opacity 0.0-1.0 (default 1.0; Krita stores
    round(opacity*255)); blend_mode (a Krita id, default "normal": e.g.
    "multiply", "screen", "add" or "linear_dodge", which render the same);
    locked, alpha_locked, inherit_alpha (clipping), all default false;
    pass_through (groups only, default false); png (paint layers only: a PNG
    inside an allowed directory, at most the canvas size limits per side;
    needs an 8-bit RGBA document) placed with its top-left at canvas x, y
    (default 0, 0; only with png); device_origin (paint layers only: {x,
    y}, the origin of Krita's tile grid for the layer, as export_layers
    records it; set before the pixels are written). Pixels outside the
    canvas are kept, not clipped; PNG pixels are written byte for byte,
    colour under alpha 0 included. Unknown keys fail.

    A manifest is the manifest.json that krita_export_layers writes (or
    tools/ora_to_manifest.py): {"version": 1, "document": {...}, "layers":
    [...]}, its layers in create order, each with a unique "key". Parents
    and "above" are "$key" refs only. Each "png" is relative to the
    manifest's directory and must still resolve inside an allowed
    directory. Entries marked "projection": true are skipped. Errors name
    the manifest entry: "layers[i]: ..." with details.index = i.

    new_document=true (manifest only, instead of doc) first creates the
    document the manifest's "document" block describes (size, resolution,
    colour profile; no layer of its own), builds into it and returns its
    handle as doc: a full rebuild in one call, with no document-level
    differences from the source. On failure the new document is closed
    again.

    Returns {doc, created: [{key, id, name, type, path}], counts} (counts
    per Krita type plus "total"). created is listed for up to 20 layers
    (verbose=true: always; false: never); a bigger batch returns
    created_listed: false instead, and krita_list_layers finds the layers.
    With dry_run, {doc, would_create: [{key, name, type, parent, above,
    png?, x?, y?, w?, h?, device_origin?}], counts} (the first 20 unless
    verbose=true; would_create_listed says how many when cut) and nothing
    changes. With new_document, also new_document {name, width, height,
    resolution, color_profile}. With a manifest it also has manifest (resolved path), skipped
    ([{index, key, reason}]) and warnings ([{code, message, ...}], e.g.
    "document_size" when the manifest's document size differs from this
    document's; not an error). One successful call is one agent undo step.

    Args:
        specs: The layers to create, 1 to 2000, in order (see above)
        manifest: Path of a manifest.json (absolute, ~, or relative to the
                  output dir), inside an allowed directory; instead of specs
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        new_document: Create the document from the manifest first (see
                      above); not with doc or specs
        name: The new document's name (new_document only); default the
              manifest's
        dry_run: Only validate and report what would be created
        verbose: List every created layer (true), none (false), or up to 20
                 (default)
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    if (specs is None) == (manifest is None):
        raise ToolError("bad_request: pass exactly one of specs and manifest")
    if new_document and manifest is None:
        raise ToolError("bad_request: new_document needs a manifest (its document block gives "
                        "the canvas size); with specs, call krita_new_canvas first")
    if new_document and doc is not None:
        raise ToolError("bad_request: pass either doc or new_document, not both")
    if manifest is not None:
        params = {"manifest": manifest}
    else:
        params = {"specs": [(s if isinstance(s, LayerSpec) else LayerSpec.model_validate(s))
                            .to_params() for s in specs]}
    params["verbose"] = "auto" if verbose is None else verbose
    result = client.call("create_layers", _with(params, doc=doc, dry_run=dry_run or None,
                                                new_document=new_document or None, name=name),
                         timeout_s=timeout_s)
    counts = result.get("counts") or {}
    total = counts.get("total", len(result.get("created") or result.get("would_create") or []))
    groups = counts.get("grouplayer", 0)
    what = f"{total} layer{'s' if total != 1 else ''}"
    if groups:
        what += f" ({groups} group{'s' if groups != 1 else ''})"
    where = _in(result) or f" in {doc or 'the active document'}"
    verb = "Would create " if result.get("dry_run") else "Created "
    if result.get("new_document") and not result.get("dry_run"):
        nd = result["new_document"]
        where = (f" in new document {result.get('doc')} ({nd.get('width')}x{nd.get('height')}, "
                 f"{nd.get('resolution')} dpi)")
    elif result.get("new_document"):
        where = " in a new document"
    summary = verb + what + where
    if manifest is not None:
        summary += f" from {result.get('manifest') or manifest}"
        notes = []
        skipped, warnings = result.get("skipped") or [], result.get("warnings") or []
        if skipped:
            notes.append(f"{len(skipped)} entr{'ies' if len(skipped) != 1 else 'y'} skipped")
        if warnings:
            texts = [str(w.get("message", w)) if isinstance(w, dict) else str(w)
                     for w in warnings]
            notes.append(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}: "
                         + "; ".join(" ".join(t.split()) for t in texts))  # one line
        if notes:
            summary += " (" + ", ".join(notes) + ")"
    if result.get("dry_run"):
        summary += " (dry run, nothing changed)"
    return _done(result, summary)


def _count(n, one, many=None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _warnings_note(warnings) -> str:
    """" (2 warnings: …)" on one line, or ""."""
    if not warnings:
        return ""
    texts = [str(w.get("message", w)) if isinstance(w, dict) else str(w) for w in warnings]
    return (f" ({_count(len(warnings), 'warning')}: "
            + "; ".join(" ".join(t.split()) for t in texts) + ")")


def _as_params(items, model) -> list:
    return [(i if isinstance(i, model) else model.model_validate(i)).to_params() for i in items]


@mcp.tool()
def krita_update_layers(changes: list[LayerChange], doc: Optional[str] = None,
                        dry_run: bool = False, timeout_s: Optional[float] = None) -> dict:
    """
    Change layer properties in one all-or-nothing call. Each change is {id,
    name?, visible?, opacity?, blend_mode?, locked?, alpha_locked?,
    inherit_alpha?, pass_through?}: id is a layer ref (UUID, name path such
    as "Head/Eyes", or unique name) and only the fields given change; at
    least one is needed. opacity is 0.0-1.0 (Krita stores
    round(opacity*255)); blend_mode is a Krita id such as "normal",
    "multiply", "screen", "add" or "linear_dodge" (these two render the
    same); inherit_alpha is clipping; pass_through is for groups only.
    Unknown keys fail, and so does naming the same layer in two changes.

    Every change is validated before anything changes; the first bad one
    fails the call with details.index. A rename to a name another node of
    the same kind has (layer and layer, group and group) is allowed but
    listed in warnings ({code: "duplicate_name", id, name, others}): a
    bare-name ref to it becomes ambiguous. A name shared between a group
    and a layer (the Face group holding the Face layer) is allowed by the
    naming rules and lint, so it is only an info note in notes ({code:
    "name_shared_across_kinds", id, name, others, message}).

    Returns {doc, updated: [{id, path, changed: [fields set]}], warnings,
    notes?}; with dry_run, {doc, would_update: [{id, path, set: {field:
    value}}], warnings, notes?} and nothing changes. One successful call
    is one agent undo step.

    Args:
        changes: The changes, 1 to 2000, applied in order
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report what would change
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = {"changes": _as_params(changes, LayerChange)}
    result = client.call("update_layers", _with(params, doc=doc, dry_run=dry_run or None),
                         timeout_s=timeout_s)
    items = result.get("updated") or result.get("would_update") or []
    verb = "Would update " if result.get("dry_run") else "Updated "
    summary = (verb + _count(len(items), "layer") + (_in(result) or f" in {doc or 'the active document'}")
               + _warnings_note(result.get("warnings")))
    if result.get("dry_run"):
        summary += " (dry run, nothing changed)"
    return _done(result, summary)


@mcp.tool()
def krita_move_layers(moves: list[Move], doc: Optional[str] = None, dry_run: bool = False,
                      timeout_s: Optional[float] = None) -> dict:
    """
    Reorder and reparent layers in one all-or-nothing call. Each move is
    {id, parent?, above? | below? | index?} with at most one position field:
    - parent: the target group (a layer ref) or "/" for the root; default
      the layer's current parent.
    - above / below: a sibling under the target parent (a ref) to go
      directly above / below.
    - index: the position from the BOTTOM (0) among the target parent's
      other children (the moving layer not counted), up to their count
      (= on top). Same counting as list_layers' index.
    - none of them: on top of the target parent's children.
    The target parent must be the root or a group; moving a group into
    itself or its own subtree fails with bad_request. Moves are applied in
    list order and each is validated against the tree as the earlier moves
    leave it (refs are resolved against the document as it was before the
    call). A moved layer keeps its id, pixels, bounds and properties, and a
    group its whole subtree; moves never shift pixels.

    Returns {doc, moved: [{id, path, parent, index}]}: each layer's place
    after the call (parent "/" or a group id, index from the bottom); with
    dry_run, {doc, would_move: [...]} with the same fields, and nothing
    changes. One successful call is one agent undo step.

    Args:
        moves: The moves, 1 to 2000, applied in order (see above)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report where the layers would go
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = {"moves": _as_params(moves, Move)}
    result = client.call("move_layers", _with(params, doc=doc, dry_run=dry_run or None),
                         timeout_s=timeout_s)
    items = result.get("moved") or result.get("would_move") or []
    verb = "Would move " if result.get("dry_run") else "Moved "
    summary = (verb + _count(len(items), "layer") + (_in(result) or f" in {doc or 'the active document'}")
               + _warnings_note(result.get("warnings")))
    if result.get("dry_run"):
        summary += " (dry run, nothing changed)"
    return _done(result, summary)


@mcp.tool()
def krita_delete_layers(ids: list[str], doc: Optional[str] = None, dry_run: bool = False,
                        timeout_s: Optional[float] = None) -> dict:
    """
    Delete layers in one all-or-nothing call. Deleting a group deletes its
    whole subtree. ids are layer refs (UUID, name path such as "Head/Eyes",
    or unique name); duplicates, and layers inside a listed group, are
    folded in. The root "/" can't be deleted; an unknown ref fails with
    not_found and nothing is deleted.

    Returns {doc, deleted: [ids, subtree members included, top to bottom],
    count, warnings}; with dry_run, {doc, would_delete, count} and nothing
    changes. One successful call is one agent undo step (krita_undo), which
    puts the layers back with the same ids, pixels and properties (the
    deleted layers are kept in memory for that; a delete too big for the
    undo journal's memory cap warns "not_undoable").

    Args:
        ids: The layers to delete, 1 to 2000 refs
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report what would be deleted
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    result = client.call("delete_layers", _with({"ids": list(ids)}, doc=doc,
                                                dry_run=dry_run or None),
                         timeout_s=timeout_s)
    items = result.get("deleted") or result.get("would_delete") or []
    count = result.get("count", len(items))
    verb = "Would delete " if result.get("dry_run") else "Deleted "
    what = _count(count, "layer")
    listed = len(set(ids))
    if count > listed:
        what += " (subtrees included)"
    summary = (verb + what + (_in(result) or f" in {doc or 'the active document'}")
               + _warnings_note(result.get("warnings")))
    if result.get("dry_run"):
        summary += " (dry run, nothing changed)"
    return _done(result, summary)


@mcp.tool()
def krita_duplicate_layer(id: str, name: Optional[str] = None, parent: Optional[str] = None,
                          above: Optional[str] = None, doc: Optional[str] = None,
                          dry_run: bool = False, timeout_s: Optional[float] = None) -> dict:
    """
    Copy a layer: a paint layer with its pixels (past the canvas edge too),
    properties and masks; a group with its whole subtree. Every copied node
    gets a new id. Masks themselves can't be duplicated (conflict):
    duplicate their layer.

    Placement: by default directly above the source, in the same parent.
    parent (a group ref, or "/" for the root) puts the copy on top of that
    group's children; above (a sibling ref) puts it directly above that
    layer, which must be a child of parent (or, without parent, of the
    source's parent).

    Naming: Krita keeps the source's name, so by default the copy has the
    same name as the source; name sets another. A name another node of the
    same kind has is allowed but warned about ({code: "duplicate_name",
    id, name, others}: a bare-name ref becomes ambiguous); one a node of
    the other kind has (group vs layer) is only a note
    ("name_shared_across_kinds"). The layers inside a copied group keep
    their names ({code: "duplicate_names_in_copy", count, names}).

    Returns {doc, source (id), layer (the copy's layer object), ids (the
    copy and its subtree, top to bottom), warnings, notes?}; with dry_run,
    {doc, source, would_create: {name, type, parent, above, count},
    warnings, notes?}
    and nothing changes. One call is one agent undo step (krita_undo
    removes the copy).

    Args:
        id: The layer to copy: UUID, name path such as "Head/Eyes", or unique
            name
        name: The copy's name; default the source's name
        parent: Group to put the copy in (layer ref, or "/" for the root);
                default the source's parent
        above: Put the copy directly above this sibling (layer ref); default
               directly above the source, or on top of parent's children
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report what would be created
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = _with({"id": id}, name=name, parent=parent, above=above, doc=doc,
                   dry_run=dry_run or None)
    result = client.call("duplicate_layer", params, timeout_s=timeout_s)
    where = _in(result) or f" in {doc or 'the active document'}"
    if result.get("dry_run"):
        plan = result.get("would_create") or {}
        count = plan.get("count", 1)
        summary = (f"Would duplicate {result.get('source', id)} as {plan.get('name')!r}"
                   + (f" ({_count(count, 'layer')} with its subtree)" if count > 1 else "")
                   + where + _warnings_note(result.get("warnings"))
                   + " (dry run, nothing changed)")
    else:
        layer = result.get("layer") or {}
        count = len(result.get("ids") or [1])
        summary = (f"Duplicated {result.get('source', id)} as {layer.get('path')!r} "
                   f"({layer.get('id')})"
                   + (f", {_count(count, 'layer')} with its subtree" if count > 1 else "")
                   + where + _warnings_note(result.get("warnings")))
    return _done(result, summary)


@mcp.tool()
def krita_set_active_layer(id: str, doc: Optional[str] = None,
                           timeout_s: Optional[float] = None) -> dict:
    """
    Make a layer the document's active layer (the one selected in the
    Layers docker): any type, masks included, but not the root. This is UI
    state, not an agent undo step. Painting tools (stroke, fill,
    draw_shape, clear) called without layer paint on the active layer of
    the document they act on, and without doc they act on the ACTIVE
    document: when doc is not the active document, the result warns
    ({code: "not_active_document"}) that painting without doc still goes
    to the active document. Passing doc and layer to the painting tools
    always works.

    Returns {doc, layer (id), path, type, warnings}.

    Args:
        id: The layer: UUID, name path such as "Head/Eyes", or unique name
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 30
    """
    result = client.call("set_active_layer", _with({"id": id}, doc=doc), timeout_s=timeout_s)
    summary = (f"Active layer is now {result.get('path') or result.get('layer') or id}"
               + (_in(result) or f" in {doc or 'the active document'}")
               + _warnings_note(result.get("warnings")))
    return _done(result, summary)


def _rect_text(r) -> str:
    if not isinstance(r, dict):
        return "?"
    return f"{r.get('w')}x{r.get('h')} at ({r.get('x')}, {r.get('y')})"


@mcp.tool()
def krita_import_png(id: str, png: str, x: int = 0, y: int = 0,
                     mode: Literal["replace", "over"] = "replace", clear_outside: bool = False,
                     doc: Optional[str] = None, dry_run: bool = False,
                     timeout_s: Optional[float] = None) -> dict:
    """
    Write a PNG's pixels into an existing paint layer, with the PNG's
    top-left corner at canvas pixel (x, y). Only the PNG's rectangle
    (x, y, PNG width, PNG height) changes: pixels outside it are untouched
    unless clear_outside is true. The rectangle may extend past the canvas
    (Krita keeps those pixels). The document must be 8-bit RGBA, and the
    target a paint layer (a group, mask or filter layer fails with
    "conflict").

    Modes:
    - "replace": the layer's pixels in the rectangle become the PNG's
      exactly, alpha included (transparent PNG pixels make the layer
      transparent there).
    - "over": the PNG is composited over the existing pixels (source-over,
      like a Normal layer on top). Where the PNG is fully transparent the
      layer is unchanged; where it is opaque, or the layer is empty, the
      result is the PNG's pixel exactly; elsewhere colours are within one
      level of the exact blend.
    - clear_outside=true (replace only): the layer is cleared first, so it
      ends up containing only the PNG.

    Returns {doc, layer (id), png (resolved path), mode, clear_outside,
    changed: {x, y, w, h} (the rectangle written; with clear_outside it
    also covers the old content), bounds: the layer's new bounds or null,
    warnings}; with dry_run, would_change instead of changed, and nothing
    changes. One call is one agent undo step (krita_undo), which restores
    the old pixels of the changed rectangle (kept in memory, or in a temp
    file beyond the journal's memory cap; an import too big for its disk
    cap too warns "not_undoable").

    Args:
        id: The paint layer: a layer UUID, a name path such as "Head/Eyes",
            or a unique name
        png: PNG file to read, inside an allowed directory (absolute,
             "~"-prefixed, or relative to the output dir)
        x: Canvas x of the PNG's left edge, in pixels; may be negative
        y: Canvas y of the PNG's top edge, in pixels; may be negative
        mode: "replace" (default) or "over"
        clear_outside: With mode "replace", clear the rest of the layer
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report the rectangle that would change
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = {"id": id, "png": png, "x": x, "y": y, "mode": mode}
    result = client.call("import_png",
                         _with(params, clear_outside=clear_outside or None, doc=doc,
                               dry_run=dry_run or None),
                         timeout_s=timeout_s)
    dry = result.get("dry_run")
    rect = result.get("would_change") if dry else result.get("changed")
    name = os.path.basename(result.get("png") or png)
    how = result.get("mode", mode) + (", clear outside" if result.get("clear_outside") else "")
    summary = (("Would import " if dry else "Imported ") + f"{name} into layer "
               + f"{result.get('layer', id)} ({how}), changing {_rect_text(rect)}"
               + (_in(result) or f" in {doc or 'the active document'}")
               + _warnings_note(result.get("warnings")))
    if dry:
        summary += " (dry run, nothing changed)"
    return _done(result, summary)


@mcp.tool()
def krita_trim_layers(ids: list[str],
                      alpha_threshold: Annotated[int, Field(ge=1, le=255)] = 8,
                      near_px: Annotated[Optional[int], Field(ge=1, le=64)] = None,
                      doc: Optional[str] = None, dry_run: bool = False,
                      timeout_s: Optional[float] = None) -> dict:
    """
    Clear faint pixels in paint layers: every pixel with 0 < alpha <
    alpha_threshold becomes fully transparent, all four bytes zeroed
    (colour too, so no hidden colour stays under the cleared alpha). Pixels
    with alpha >= alpha_threshold are untouched, and so are pixels already
    at alpha 0 (not counted). The default threshold 1 clears nothing: pass
    e.g. 8 to remove alpha 1-7 fringes and specks.

    near_px (1-64) keeps anti-aliased edges: only faint pixels farther than
    near_px from every pixel with alpha >= alpha_threshold are cleared
    (Krita's Selection.grow kernel, as in lint's stray-alpha rule; a layer
    with no such pixel loses all its faint ones). Without near_px every
    faint pixel goes.

    ids are layer refs (UUID, name path, unique name); duplicates are
    folded. Every layer must be a paint layer (else conflict) and the
    document 8-bit RGBA; all are checked before anything changes. The
    whole layer is trimmed, pixels past the canvas edge included.

    Returns {doc, alpha_threshold, near_px, trimmed: [{id, path, cleared
    (pixel count), bounds_before, bounds_after}], totals: {layers, changed,
    cleared}, warnings}; with dry_run, would_trim with the same entries
    and nothing changes. One call is one agent undo step (krita_undo
    restores the old pixels exactly; a trim too big for the journal's caps
    warns "not_undoable").

    Args:
        ids: Paint layers to trim, 1 to 2000 refs
        alpha_threshold: Pixels with alpha below this (1-255) are cleared;
                         default 8 (lint's stray_alpha_below)
        near_px: Keep faint pixels within this many pixels (1-64) of a pixel
                 at or above the threshold; default null (clear them all)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only count what would be cleared
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = _with({"ids": list(ids), "alpha_threshold": alpha_threshold}, near_px=near_px,
                   doc=doc, dry_run=dry_run or None)
    result = client.call("trim_layers", params, timeout_s=timeout_s)
    dry = result.get("dry_run")
    totals = result.get("totals") or {}
    cleared, changed = totals.get("cleared", 0), totals.get("changed", 0)
    threshold = result.get("alpha_threshold", alpha_threshold)
    near = result.get("near_px", near_px)
    summary = (("Would clear " if dry else "Cleared ") + _count(cleared, "pixel")
               + f" in {changed} of {_count(totals.get('layers', len(ids)), 'layer')}"
               + f" (alpha < {threshold}"
               + (f", farther than {near} px from stronger pixels" if near else "") + ")"
               + (_in(result) or f" in {doc or 'the active document'}")
               + _warnings_note(result.get("warnings")))
    if dry:
        summary += " (dry run, nothing changed)"
    return _done(result, summary)


@mcp.tool()
def krita_export_layers(dir: str, ids: Union[Literal["all"], list[str]] = "all",
                        crop: bool = True, include_hidden: bool = True,
                        include_groups: bool = False, overwrite: bool = False,
                        png_level: Annotated[Optional[int], Field(ge=0, le=9)] = None,
                        verbose: bool = False,
                        doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Export layers as PNGs plus a manifest.json into a directory, without
    changing the document. The manifest feeds krita_create_layers
    (manifest=<dir>/manifest.json) on another canvas to rebuild the same
    layer tree: same names, nesting, order, visibility, opacity, blend
    modes, locks, clipping (inherit_alpha), pass-through and pixels. The
    document must be 8-bit RGBA.

    - One PNG per paint layer, the layer's own pixels bit for bit (its
      opacity, blend mode and visibility are in the manifest, not baked
      in), as straight (non-premultiplied) RGBA. With crop (default) each
      PNG is cut to the pixels that hold anything (alpha > 0, or colour
      under alpha 0 next to them), which may lie partly off the canvas, and
      the manifest gives its x, y, w, h; with crop=false each PNG is
      canvas-sized at (0, 0), and pixels past the canvas edge are lost
      (warning "off_canvas").
    - Each paint layer's entry also has device_origin {x, y}, the origin of
      Krita's tile grid for that layer; krita_create_layers sets it again,
      which is what makes the rebuilt composite identical to the source
      (without it Krita's compositing can round soft edges by 1).
    - The manifest's "document" block (width, height, resolution, colour
      model, depth and profile, name) lets krita_create_layers(manifest=…,
      new_document=true) rebuild the whole document in one call.
    - Every group gets a manifest entry (no PNG). A paint layer with no
      pixels gets an entry with png null, so the structure round-trips.
    - Order: the manifest lists layers in create order (depth-first, each
      parent before its children, siblings bottom to top), with keys
      L0001, L0002, ... and stack_index 0, 1, ... in that order (0 draws
      first); PNG files are named <number>_<name>.png after the key
      number, unique even when names repeat.
    - Hidden layers are exported (with visible false) unless
      include_hidden=false, which leaves them out, a hidden group with its
      contents (listed in skipped).
    - include_groups=true also writes each group's projection (the group
      composited on its own, as if isolated and visible, without its own
      opacity) as a separate entry marked "projection": true, after the
      group's contents; krita_create_layers skips those entries.
    - ids: "all" (default) or a list of layer refs. A listed group brings
      its whole subtree; the groups around a listed layer are written as
      group entries (no projection) so the nesting is kept.
    - Masks, and filter, fill, clone, vector and file layers, are not
      exported (skipped, with warnings "masks_not_applied" /
      "not_exported": a rebuild won't render the same). A paint layer
      with masks is exported without the masks applied.
    - dir must be inside an allowed directory; it is created if missing
      and must be empty unless overwrite=true.

    Cost is mostly PNG encoding (about 1.7 s per full-canvas image at
    5800x8400); png_level=1 is much faster than the default for a few
    percent more bytes.

    Returns {doc, dir, manifest (path), count (PNGs written), counts
    ({paint, group, projection, png, skipped}), skipped ([{id, path, type,
    reason}]), warnings ([{code, message, ...}]), bytes (total written)};
    with verbose=true also layers (the manifest entries, which are in the
    manifest file either way).

    Args:
        dir: Output directory (absolute, "~"-prefixed, or relative to the
             output dir), inside an allowed directory
        ids: "all", or a list of layer refs (UUIDs, name paths such as
             "Head/Eyes", or unique names)
        crop: Cut each PNG to the layer's opaque bounds (default true)
        include_hidden: Export hidden layers too (default true)
        include_groups: Also write group projections (default false)
        overwrite: Write into a non-empty directory (default false)
        png_level: zlib level 0 (fastest, biggest) to 9 (smallest), or
                   null for the default (6)
        verbose: Also return every manifest entry (default false)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    if isinstance(ids, str):
        if ids != "all":
            raise ToolError(f'bad_request: ids must be "all" or a list of layer refs, got {ids!r}')
        ids_param = None
    else:
        ids_param = list(ids)
    if png_level is not None and (isinstance(png_level, bool) or not isinstance(png_level, int)
                                  or not 0 <= png_level <= 9):
        raise ToolError(f"bad_request: png_level must be a whole number from 0 to 9, "
                        f"got {png_level!r}")
    params = _with({"dir": dir}, ids=ids_param, crop=None if crop else False,
                   include_hidden=None if include_hidden else False,
                   include_groups=include_groups or None, overwrite=overwrite or None,
                   png_level=png_level, doc=doc)
    params["verbose"] = verbose
    result = client.call("export_layers", params, timeout_s=timeout_s)
    counts = result.get("counts")
    if isinstance(counts, dict):
        paint, groups = counts.get("paint", 0), counts.get("group", 0)
        projections = counts.get("projection", 0)
    else:  # an entry list only (an older plugin)
        layers = result.get("layers") or []
        groups = sum(1 for e in layers if e.get("type") == "group")
        projections = sum(1 for e in layers if e.get("projection"))
        paint = len(layers) - groups - projections
    count = result.get("count", 0)
    what = f"{_count(count, 'PNG')} for {_count(paint, 'layer')} and {_count(groups, 'group')}"
    if projections:
        what += f" ({_count(projections, 'group projection')})"
    summary = (f"Exported {what}" + (_in(result) or f" from {doc or 'the active document'}")
               + f" to {result.get('dir') or dir}")
    if result.get("bytes") is not None:
        summary += f", {_size(result['bytes'])}"
    skipped = result.get("skipped") or []
    if skipped:
        summary += f", {len(skipped)} skipped"
    return _done(result, summary + _warnings_note(result.get("warnings")))


# Full-size renders above this many pixels get the cost hint in the summary.
_RENDER_HINT_PIXELS = 4_000_000


def _render_summary(result: dict, path: str, what: str = "Rendered {doc}",
                    what_no_doc: str = "Rendered", via_render: bool = False) -> str:
    """"Rendered doc-2 to <path>: 1450x2100 (scale 0.25 of …), 1.2 MB", plus
    the cost hint for big full-size renders, unless png_level 0 or 1 was
    already used (F004 K10). ``via_render``: the tool has neither option
    (get_canvas), so the hint names krita_render."""
    w, h = result.get("width"), result.get("height")
    summary = what.format(doc=result["doc"]) if result.get("doc") else what_no_doc
    summary += f" to {result.get('path') or path}"
    if isinstance(w, int) and isinstance(h, int):
        summary += f": {w}x{h}"
    region = result.get("region")
    scale = result.get("scale", 1.0)
    details = []
    if isinstance(region, dict) and (w, h) != (region.get("w"), region.get("h")):
        details.append(f"scale {scale:g} of {_rect_text(region)}")
    elif isinstance(region, dict) and (region.get("x"), region.get("y")) != (0, 0):
        details.append(f"region {_rect_text(region)}")
    if result.get("only") is not None:
        details.append(f"only {_count(len(result['only']), 'layer')}")
    if result.get("hide") is not None:
        details.append(f"{_count(len(result['hide']), 'layer')} hidden")
    if result.get("background"):
        details.append(f"over {result['background']}")
    if details:
        summary += " (" + ", ".join(details) + ")"
    if result.get("bytes") is not None:
        summary += f", {_size(result['bytes'])}"
    level = result.get("png_level")
    fast = isinstance(level, int) and not isinstance(level, bool) and level <= 1
    if (scale == 1 and not fast and isinstance(w, int) and isinstance(h, int)
            and w * h >= _RENDER_HINT_PIXELS):
        summary += ("; full-size PNG encoding is slow (about 4 s at 5800x8400): "
                    + ("use krita_render with " if via_render else "pass ")
                    + "scale=0.25 for previews, or png_level=1")
    return summary


@mcp.tool()
def krita_render(path: str, region: Optional[Rect] = None,
                 scale: Annotated[float, Field(gt=0, le=4)] = 1.0,
                 only: Optional[list[str]] = None, hide: Optional[list[str]] = None,
                 background: Optional[str] = None,
                 png_level: Annotated[Optional[int], Field(ge=0, le=9)] = None,
                 doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Render a document (what it looks like, all visible layers composited)
    to a PNG file, optionally a region of it, scaled, with only some layers
    shown, over a background colour. The user's document is never changed:
    renders with only/hide are made in a private copy of the document,
    closed afterwards, so no visibility flag or modified state changes.

    - region: {x, y, w, h} in canvas pixels (origin top-left), default the
      whole canvas; clipped to the canvas (a region entirely off the canvas
      fails with "bad_request"). The result's region is the clipped one.
    - scale: 0 < scale <= 4. The PNG is round(w * scale) x round(h * scale)
      pixels (each at least 1), resampled smoothly; it may not exceed the
      canvas pixel limit (max_canvas_pixels, "too_large"). At scale 1 the
      PNG holds Krita's composited pixels exactly.
    - only: layer refs to show; everything else is hidden. The ancestors of
      each listed layer are shown too (even if hidden), and the layers
      inside a listed group keep their own visibility (a hidden child stays
      hidden). hide: layer refs to hide. Both together: only these, minus
      those. Empty lists fail with "bad_request"; unknown refs "not_found",
      names matching several layers "ambiguous".
    - background: null for a transparent PNG (default), or a colour
      ("#rrggbb", "#rgb", "#aarrggbb" or an SVG name) composited
      underneath.
    - Any colour space works: Krita converts the image to 8-bit sRGB (for
      an 8-bit sRGB document, the exact pixels).

    Cost is mostly PNG encoding: a full-size 5800x8400 render takes about
    4 s (1.7 s with png_level=1), and at scale=0.25 about 0.4 s. Use
    scale=0.25 (or smaller) for previews.

    Returns {doc, path (resolved), width, height (of the PNG), region (the
    clipped canvas rectangle rendered), scale, bytes, and only, hide
    (resolved ids), background, png_level when given}.

    Args:
        path: PNG file to write (must end in .png): absolute, "~"-prefixed
              or relative to the output dir, inside an allowed directory;
              missing parent directories are created, an existing file is
              replaced
        region: {x, y, w, h} canvas rectangle to render; default the
                whole canvas
        scale: Output scale, greater than 0 and at most 4 (default 1)
        only: Layer refs (UUIDs, name paths such as "Head/Eyes", or unique
              names) to show, hiding everything else
        hide: Layer refs to hide
        background: Colour under the image, or null for transparent
        png_level: zlib level 0 (fastest, biggest) to 9 (smallest), or
                   null for the default (6)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    if (isinstance(scale, bool) or not isinstance(scale, (int, float))
            or not 0 < scale <= 4):
        raise ToolError(f"bad_request: scale must be greater than 0 and at most 4, got {scale!r}")
    if png_level is not None and (isinstance(png_level, bool) or not isinstance(png_level, int)
                                  or not 0 <= png_level <= 9):
        raise ToolError(f"bad_request: png_level must be a whole number from 0 to 9, "
                        f"got {png_level!r}")
    region_param = None
    if region is not None:
        try:
            region_param = (region if isinstance(region, Rect)
                            else Rect.model_validate(region)).to_params()
        except ValidationError as e:
            raise ToolError(f"bad_request: region must be {{x, y, w, h}} whole numbers with "
                            f"w and h at least 1: {e.errors()[0].get('msg', e)}") from None
    params = _with({"path": path}, region=region_param, scale=None if scale == 1 else scale,
                   only=None if only is None else list(only),
                   hide=None if hide is None else list(hide), background=background,
                   png_level=png_level, doc=doc)
    result = client.call("render", params, timeout_s=timeout_s)
    return _done(result, _render_summary(result, path))


# --- Selections (api-design § Selections and part separation) ----------------

SelectMode = Literal["rect", "polygon", "alpha_of", "invert", "none", "all"]
SelectOp = Literal["replace", "add", "subtract", "intersect"]
Radius = Annotated[Optional[int], Field(ge=1, le=500)]
Point = Annotated[list[float], Field(min_length=2, max_length=2)]


def _select_summary(result: dict, mode: str, doc: Optional[str]) -> str:
    where = _in(result) or f" in {doc or 'the active document'}"
    mode = result.get("mode", mode)
    count, bounds = result.get("pixel_count"), result.get("bounds")
    if mode == "none":
        return f"Selection cleared{where}"
    if not count or not bounds:
        return (f"Nothing selected{where} ({mode}, {result.get('op', 'replace')}); the "
                "document has no selection now")
    return (f"Selected {_count(count, 'pixel')} in {_rect_text(bounds)}{where} "
            f"({mode}, {result.get('op', 'replace')})")


@mcp.tool()
def krita_select(mode: SelectMode, op: SelectOp = "replace",
                 x: Optional[int] = None, y: Optional[int] = None,
                 w: Annotated[Optional[int], Field(ge=1)] = None,
                 h: Annotated[Optional[int], Field(ge=1)] = None,
                 points: Annotated[Optional[list[Point]],
                                   Field(min_length=3, max_length=10000)] = None,
                 antialias: Optional[bool] = None, layer: Optional[str] = None,
                 threshold: Annotated[Optional[int], Field(ge=1, le=255)] = None,
                 grow: Radius = None, shrink: Radius = None, feather: Radius = None,
                 doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Set the document's selection (what Krita's selection tools set, shown as
    marching ants; krita_selection_to_layer cuts it into a new layer).
    Coordinates are canvas pixels, origin top-left; selections may extend
    past the canvas.

    Modes, each with its own parameters (others fail with "bad_request"):
    - "rect": x, y, w, h. The pixels x..x+w-1, y..y+h-1.
    - "polygon": points, 3 to 10000 [x, y] (numbers; pixel corners, so
      [[2, 3], [6, 3], [6, 8], [2, 8]] is the rect x=2, y=3, w=4, h=5),
      antialias (default true: edge pixels get partial values; false:
      every pixel is fully in or out). Filled with the non-zero winding
      rule: overlapping loops stay selected (a pentagram's centre too).
    - "alpha_of": layer, threshold (1-255, default 1): the pixels where the
      layer's alpha >= threshold, fully selected. A paint layer's own
      pixels; a group (or other layer) its projection; masks fail
      ("conflict"). The document must be 8-bit RGBA.
    - "invert": the canvas pixels not selected (255 - value); anything past
      the canvas is dropped. With no selection, the whole canvas.
    - "none": deselect.
    - "all": the whole canvas.

    op combines the new selection with the current one: "replace"
    (default), "add" (values summed, capped at full), "subtract" (the new
    one's values taken off), "intersect" (the smaller value). For 0/full
    selections that is union, difference and intersection; subtract or
    intersect with no current selection selects nothing. "invert" and
    "none" take only "replace".

    Then, in this order: grow (dilate by that many pixels, 1-500), shrink
    (erode; the canvas edge doesn't stop it: outside the selection counts
    as unselected), feather (blur the edge over that radius: soft, partial
    values that krita_selection_to_layer turns into soft alpha). "none"
    takes none of them.

    Returns {doc, mode, op, bounds: {x, y, w, h} or null, pixel_count
    (pixels selected at all, value > 0), warnings}; a result that selects
    nothing deselects ({code: "empty_selection"}, bounds null). The same
    bounds show up as krita_get_document_info's selection.

    Selections are not agent edits: krita_undo doesn't undo a select (it
    is not in the agent journal); select again, or mode="none".

    Args:
        mode: "rect", "polygon", "alpha_of", "invert", "none" or "all"
        op: "replace" (default), "add", "subtract" or "intersect"
        x: rect: left edge in canvas pixels; may be negative
        y: rect: top edge in canvas pixels; may be negative
        w: rect: width in pixels, at least 1
        h: rect: height in pixels, at least 1
        points: polygon: [[x, y], ...] vertices in canvas pixels, 3 to 10000
        antialias: polygon: soft (partial) edge pixels; default true
        layer: alpha_of: the layer whose alpha to select: its id (UUID), a
               name path such as "Head/Eyes", or a unique name
        threshold: alpha_of: minimum alpha (1-255) of a selected pixel;
                   default 1 (any visible pixel)
        grow: Grow the result by this many pixels (1-500)
        shrink: Then shrink it by this many pixels (1-500)
        feather: Then feather its edge over this radius (1-500)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 30
    """
    params = _with({"mode": mode, "op": op}, x=x, y=y, w=w, h=h,
                   points=None if points is None else [list(p) for p in points],
                   antialias=antialias, layer=layer, threshold=threshold, grow=grow,
                   shrink=shrink, feather=feather, doc=doc)
    result = client.call("select", params, timeout_s=timeout_s)
    return _done(result, _select_summary(result, mode, doc) + _warnings_note(
        [w for w in result.get("warnings") or [] if not (isinstance(w, dict)
                                                         and w.get("code") == "empty_selection")]))


@mcp.tool()
def krita_selection_to_layer(source: str, name: str, parent: Optional[str] = None,
                             above: Optional[str] = None, cut: bool = True,
                             doc: Optional[str] = None, dry_run: bool = False,
                             timeout_s: Optional[float] = None) -> dict:
    """
    Separate a part: move (cut=true, default) or copy (cut=false) a paint
    layer's pixels inside the current selection (krita_select) to a new
    paint layer, at the same canvas position. Never uses Krita's clipboard
    (the user's clipboard is left alone), and the selection stays as it is.

    Pixels: the new layer gets the source's pixels with alpha scaled by the
    selection's value (alpha * s / 255, rounded; colours unchanged), so a
    feathered or antialiased selection gives soft edges; fully selected
    pixels are copied exactly. With cut, the source keeps alpha *
    (255 - s) / 255: fully selected pixels become fully transparent (all
    four bytes zeroed, no hidden colour left), unselected pixels are
    untouched, bit for bit. Pixels whose alpha ends at 0 have their colour
    zeroed too. Only the selection's bounds within the source's bounds are
    read and written (cut_rect).

    The new layer has default properties (normal, opacity 1, visible) and
    goes directly above the source; parent (a group ref, or "/") puts it on
    top of that group's children, above (a sibling ref) directly above that
    layer (a child of parent, or without parent of the source's parent).
    A name another layer has is allowed but warned about
    ({code: "duplicate_name"}); a group's name only gives a note in notes
    ("name_shared_across_kinds").

    Fails with "conflict" when there is no selection, the source is not a
    paint layer, or the selection doesn't overlap the layer; the document
    must be 8-bit RGBA.

    Returns {doc, source, layer (the new layer object), cut_rect: {x, y,
    w, h}, cut, copied (pixels with alpha > 0 in the new layer), warnings
    ("nothing_copied" when that is 0)}; with dry_run, {doc, source,
    cut_rect, cut, would_create: {name, type, parent, above}, warnings}
    and nothing changes. One call is one agent undo step: krita_undo
    removes the new layer and restores the source's pixels.

    Args:
        source: The paint layer to take pixels from: UUID, name path such as
                "Head/Eyes", or unique name
        name: The new layer's name
        parent: Group to put the new layer in (layer ref, or "/" for the
                root); default the source's parent
        above: Put the new layer directly above this sibling (layer ref);
               default directly above the source
        cut: Remove the pixels from the source (default true); false copies
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report the rectangle and the new layer
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = _with({"source": source, "name": name, "cut": cut}, parent=parent, above=above,
                   doc=doc, dry_run=dry_run or None)
    result = client.call("selection_to_layer", params, timeout_s=timeout_s)
    where = _in(result) or f" in {doc or 'the active document'}"
    verb = "cut" if result.get("cut", cut) else "copy"
    rect = _rect_text(result.get("cut_rect"))
    if result.get("dry_run"):
        plan = result.get("would_create") or {}
        summary = (f"Would {verb} the selection ({rect}) of {result.get('source', source)} to a "
                   f"new layer {plan.get('name', name)!r}{where}"
                   + _warnings_note(result.get("warnings")) + " (dry run, nothing changed)")
    else:
        layer = result.get("layer") or {}
        done = "Cut" if verb == "cut" else "Copied"
        summary = (f"{done} {_count(result.get('copied', 0), 'pixel')} ({rect}) of "
                   f"{result.get('source', source)} to new layer {layer.get('path', name)!r} "
                   f"({layer.get('id')}){where}" + _warnings_note(result.get("warnings")))
    return _done(result, summary)


# --- Transform and merge (api-design § krita_transform_layer, § krita_merge_layers)

Resample = Literal["Bicubic", "Bilinear", "NearestNeighbor", "Lanczos3", "Mitchell", "BSpline",
                   "Bell", "Hermite"]
ScaleFactor = Annotated[float, Field(ge=0.01, le=100)]


def _step_text(step) -> str:
    if not isinstance(step, dict):
        return str(step)
    op = step.get("op")
    if op == "flip":
        return f"flip {step.get('axis')}"
    if op == "scale":
        size = step.get("size") or {}
        return f"scale to {size.get('w')}x{size.get('h')}"
    if op == "rotate":
        return f"rotate {step.get('degrees'):g}°"
    if op == "translate":
        by = step.get("by") or ["?", "?"]
        return f"translate by ({by[0]}, {by[1]})"
    return str(op)


@mcp.tool()
def krita_transform_layer(id: str, translate: Optional[tuple[int, int]] = None,
                          scale: Optional[Union[ScaleFactor, tuple[ScaleFactor, ScaleFactor]]] = None,
                          rotate: Annotated[Optional[float], Field(ge=-3600, le=3600)] = None,
                          flip: Optional[Literal["h", "v"]] = None,
                          pivot: Optional[Union[Literal["center"], tuple[float, float]]] = None,
                          resample: Resample = "Bicubic", doc: Optional[str] = None,
                          dry_run: bool = False, timeout_s: Optional[float] = None) -> dict:
    """
    Move, mirror, resize or turn a paint layer, or a group of paint layers
    (its layers keep their places relative to each other). Steps run in
    this fixed order: flip, scale, rotate, translate. Coordinates are canvas
    pixels, origin top-left.

    - flip: "h" mirrors left-right, "v" top-bottom. Exact (no resampling):
      the pixels are mirrored about the pivot's vertical ("h") or
      horizontal ("v") line, by default the layer's own centre, so the
      layer stays in place. A custom pivot moves it to the mirrored place
      (the axis is rounded to the nearest half pixel).
    - scale: one factor, or [sx, sy] (0.01-100 each), about the pivot (it
      stays fixed). The new size is the bounds times the factors, rounded.
      resample picks Krita's filter (default "Bicubic"; "NearestNeighbor"
      keeps hard pixels).
    - rotate: degrees, positive = clockwise on screen, about the pivot.
      Quarter turns (90, 180, 270) are exact pixel permutations; other
      angles use Krita's own (bicubic) rotation, whatever resample says.
    - translate: [dx, dy] whole pixels, relative. Exact.
    - pivot: "center" (default: the centre of the layer's bounds before
      the call) or [x, y] in canvas pixels.

    Masks, filter / fill / clone / vector / file layers, a paint layer with
    masks and a group holding any of those fail with "conflict" (the undo
    could not restore them), as does a layer with no pixels. The document
    must be 8-bit RGBA unless the call only translates.

    Returns {doc, layer (id), path, type, bounds_before, bounds_after
    ({x, y, w, h}), applied (the steps done, with their pixel shifts and
    Krita's scale factors), warnings}; with dry_run, {doc, layer,
    bounds_before, would_apply (each step with its predicted bounds),
    bounds_predicted, warnings} and nothing changes (predictions can be
    off by a pixel). One call is one agent undo step: krita_undo restores
    the pixels and bounds exactly.

    Args:
        id: The layer or group to transform: UUID, name path such as
            "Head/Eyes", or unique name
        translate: [dx, dy], whole canvas pixels, applied last
        scale: A factor, or [sx, sy], each 0.01-100 (1 keeps the size)
        rotate: Degrees, clockwise on screen (-3600 to 3600)
        flip: "h" (mirror left-right) or "v" (top-bottom), applied first
        pivot: "center" (default) or [x, y] in canvas pixels: the point that
               flip, scale and rotate keep fixed
        resample: Krita's scaling filter: Bicubic (default), Bilinear,
                  NearestNeighbor, Lanczos3, Mitchell, BSpline, Bell, Hermite
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and predict the bounds
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    if scale is not None and not isinstance(scale, (int, float)):
        scale = list(scale)
    params = _with({"id": id, "resample": resample},
                   translate=None if translate is None else list(translate), scale=scale,
                   rotate=rotate, flip=flip,
                   pivot=pivot if pivot is None or isinstance(pivot, str) else list(pivot),
                   doc=doc, dry_run=dry_run or None)
    result = client.call("transform_layer", params, timeout_s=timeout_s)
    where = _in(result) or f" in {doc or 'the active document'}"
    what = f"{result.get('path', id)!r}"
    before = _rect_text(result.get("bounds_before"))
    if result.get("dry_run"):
        steps = ", ".join(_step_text(s) for s in result.get("would_apply") or []) or "nothing"
        summary = (f"Would transform {what} ({steps}){where}: {before} -> about "
                   f"{_rect_text(result.get('bounds_predicted'))} (dry run, nothing changed)")
    else:
        steps = ", ".join(_step_text(s) for s in result.get("applied") or []) or "nothing to do"
        summary = (f"Transformed {what} ({steps}){where}: {before} -> "
                   f"{_rect_text(result.get('bounds_after'))}")
    return _done(result, summary + _warnings_note(result.get("warnings")))


@mcp.tool()
def krita_merge_layers(ids: Annotated[list[str], Field(min_length=1)], name: str,
                       doc: Optional[str] = None, dry_run: bool = False,
                       timeout_s: Optional[float] = None) -> dict:
    """
    Merge layers into one new paint layer named name.

    - One group: flatten it in place. The pixels are the group's composite
      (clipping, blend modes, filters, masks and layer styles inside it are
      baked in); the new layer keeps the group's opacity, blend mode,
      visibility, clipping and lock as properties, so the document renders
      the same. A pass-through group is flattened to its isolated composite
      (warning "pass_through_flattened": its blend modes no longer reach
      the layers below it). Hidden groups work too (the new layer is
      hidden).
    - Several layers with the same parent (else "bad_request"): merged, in
      their stacking order, as if grouped at the topmost one's place, then
      flattened. Their opacities and blend modes are baked into the pixels;
      the new layer is normal, opacity 1, visible. Warnings: layers with a
      blend mode other than normal only blend with the merged layers below
      them ("isolated_compositing"), a clipped lowest layer loses its base
      ("clipping_base_not_merged"), hidden layers drop out
      ("hidden_layers_dropped"), layers in between that are not merged end
      up below ("not_adjacent").

    The new layer takes the (topmost) merged layer's place; the merged
    layers are removed with their subtrees. Masks can't be merged on their
    own ("conflict"). Nothing temporary is left in the document.

    Returns {doc, mode ("group" or "layers"), merged (the ids, top first),
    layer (the new layer object), removed (every removed id, subtrees
    included, top to bottom), warnings}; with dry_run, {doc, mode, merged,
    would_create: {name, type, parent, above}, would_remove, warnings} and
    nothing changes. One call is one agent undo step: krita_undo puts the
    original layers back exactly (same ids, pixels and properties).

    Args:
        ids: One group, or several sibling layers: UUIDs, name paths such
             as "Head/Eyes", or unique names
        name: The new layer's name
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        dry_run: Only validate and report what would be merged
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = _with({"ids": list(ids), "name": name}, doc=doc, dry_run=dry_run or None)
    result = client.call("merge_layers", params, timeout_s=timeout_s)
    where = _in(result) or f" in {doc or 'the active document'}"
    merged = result.get("merged") or list(ids)
    what = (f"group {merged[0]}" if result.get("mode") == "group"
            else _count(len(merged), "layer"))
    if result.get("dry_run"):
        removed = result.get("would_remove") or []
        verb = "flatten" if result.get("mode") == "group" else "merge"
        summary = (f"Would {verb} {what} into a new layer {name!r}{where}, removing "
                   f"{_count(len(removed), 'layer')}" + _warnings_note(result.get("warnings"))
                   + " (dry run, nothing changed)")
    else:
        layer = result.get("layer") or {}
        verb = "Flattened" if result.get("mode") == "group" else "Merged"
        summary = (f"{verb} {what} into {layer.get('path', name)!r} ({layer.get('id')}){where}, "
                   f"removing {_count(len(result.get('removed') or []), 'layer')}"
                   + _warnings_note(result.get("warnings")))
    return _done(result, summary)


# --- Analysis (api-design § Analysis) ----------------------------------------

def _lint_summary(result: dict, doc: Optional[str]) -> str:
    """"3 errors, 1 warning in doc-2 (10 rules)", or "No findings in …"."""
    counts = result.get("counts") or {}
    parts = [_count(counts[s], s, "info" if s == "info" else None)
             for s in ("error", "warning", "info")
             if isinstance(counts.get(s), int) and counts[s]]
    where = _in(result) or f" in {doc or 'the active document'}"
    rules_run = result.get("rules_run")
    ran = f" ({_count(len(rules_run), 'rule')})" if isinstance(rules_run, list) else ""
    summary = (", ".join(parts) if parts else "No findings") + where + ran
    by_rule = result.get("by_rule")
    if isinstance(by_rule, dict) and by_rule:
        summary += ": " + ", ".join(f"{rule} {g.get('count')}" for rule, g in by_rule.items()
                                    if isinstance(g, dict))
    if result.get("pixel_stats") is False:
        summary += "; pixel rules skipped (not an 8-bit RGBA document)"
    off = result.get("rules_off")
    if isinstance(off, dict):
        live2d = off.get("live2d") or []
        disabled = off.get("disabled") or []
        if live2d:
            summary += (f"; {_count(len(live2d), 'Live2D convention rule')} off "
                        "(set live2d.enabled in the config to run them)")
        if disabled:
            summary += f"; {_count(len(disabled), 'rule')} disabled by lint.disable"
    return summary


@mcp.tool()
def krita_lint(doc: Optional[str] = None, rules: Optional[list[str]] = None,
               severity_min: Optional[Literal["info", "warning", "error"]] = None,
               group_by: Literal["rule", "none"] = "rule",
               limit: Annotated[int, Field(ge=1, le=10000)] = 5,
               offset: Annotated[int, Field(ge=0)] = 0,
               timeout_s: Optional[float] = None) -> dict:
    """
    Check a document's layers for problems before handing it on. Reads
    only; the document is not changed. Two packs of rules:

    Core rules (always available, set "default"; the config's
    lint.disable takes one out of the sets):
    - duplicate-name (warning; error with the Live2D conventions on): a
      name used by several groups, or by several layers, anywhere in the
      document (a group may share its name with a layer, e.g. the Face
      group holding the Face layer); bare-name refs to them are ambiguous.
      One finding per name, related_ids lists them all, the message gives
      their paths and a fix; with the Live2D conventions on,
      suggested_names ([{id, name}]) gives the pose-suffix names when the
      copies sit in pose groups ("Hand L A" / "Hand L B")
    - empty-layer (warning): a paint layer with no pixels
    - out-of-canvas (warning): a paint layer's pixels reach past the canvas
    - stray-alpha (warning): isolated specks of faint pixels: groups of at
      most stray_speck_max_px (default 64) px with alpha below 8, more
      than 4 px from any stronger pixel; anti-aliased edges and larger
      soft regions (glows) don't count. The finding has pixels (the specks'
      pixel count) and bounds (their box), enough to decide on
      krita_trim_layers without rendering

    Live2D rig conventions (part separation for Cubism; OFF unless the
    config's live2d.enabled is true). Set "default":
    - node-type (error): a node other than a paint layer or group: filter,
      fill, clone, vector or file layers, and every mask (bake or remove)
    - layer-style (error): a layer with a layer style (bake or remove)
    - inherit-alpha (error): a clipped layer (inherit alpha); bake the
      clipping into the pixels
    - blend-mode (error): a paint layer's blend mode outside
      live2d.allowed_blend_modes (default normal, multiply, add; add and
      linear_dodge are the same mode)
    - side-token (warning): "Eye" next to "Eye L" in one group, or a
      non-standard side token such as "Eye_L", "Left Eye", "(L)"
    - color-space (error): the document is not 8-bit RGBA sRGB (one
      document-level finding)
    Set "conventions" (opt-in even with the pack on):
    - nesting-depth (warning): a paint layer more than live2d.max_nesting
      (default 3) groups deep
    - name-case (info): a paint layer or group name that is not Title Case
      with single-space-separated words (side tokens, A/B variants,
      numbers and lowercase minor words such as "of" are allowed)
    - blend-suffix (warning): a multiply/add paint layer without the
      " Multiply"/" Add" name suffix, or a suffix that doesn't match the
      blend mode
    - pose-visibility (warning): sibling pose groups "<X> A", "<X> B", ...
      where A is hidden or another one is visible
    - reference-hidden (warning): a reference group that is visible
    - reference-unbracketed (warning): a hidden group that is neither a
      reference group nor a pose group, or a hidden top-level paint layer
      that isn't a pose variant: reference material belongs in a
      bracketed group, but hidden rig parts are fine, so the message gives
      both readings (a group's finding has suggested_names with the
      bracketed name)
    - mask-suffix (info): the base layer of a clipped (inherit alpha)
      layer whose name doesn't end in " Mask"

    The contents of reference groups (a group whose name matches
    lint.reference_pattern: by default in brackets, such as "[ Guide ]")
    are skipped by every rule. Pixel rules need an 8-bit RGBA document
    (pixel_stats false otherwise).

    Sets: "default", "conventions", "all" (every rule), each expanded to
    the rules the config enables; rules_off {reason: [rule ids]} lists the
    ones a requested set left out ("live2d": the Live2D conventions are
    off; "disabled": in lint.disable). A rule named by id always runs.

    Settings come from the plugin's config file (~/.krita-mcp-config.json)
    and are returned as settings. Section "lint": stray_alpha_below
    (default 8), stray_alpha_distance (default 4 px), stray_speck_max_px
    (default 64), reference_pattern (a regular expression; default
    "^\\[.*\\]$"), disable (rule ids). Section "live2d": enabled (default
    false), additive_mode ("either" (default), "add" or "linear_dodge":
    require one spelling of the Add mode), allowed_blend_modes,
    max_nesting, pose_pattern (a regular expression with groups stem and
    pose; default: the last word one capital other than L and R) and
    default_pose (default "A").

    Findings are {rule, severity, layer_id, path, message, related_ids?,
    suggested_names?, pixels?, bounds?}, sorted by stacking order (top to
    bottom, as in the Layers docker), document-level findings (layer_id
    and path null) first, then by rule.

    Results are grouped by rule (group_by "rule", the default) so a big
    document's lint fits in context: by_rule {rule: {severity, count,
    items (the first `limit` findings, from `offset`, without the rule
    and severity their group gives), next_offset}}.
    group_by "none" gives the flat findings list instead, paged the same
    way (offset, limit, next_offset). To see every finding of one rule,
    pass rules=["<rule>"] and a larger limit, or page with offset.

    Returns {doc, by_rule | findings, counts ({error, warning, info}, over
    all findings), total, rules_run, rules_off, settings, pixel_stats}.

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        rules: Rule ids and/or set names to run (default ["default"]); an
               unknown name fails with bad_request listing the valid ones
        severity_min: Drop findings less severe than this ("info",
                      "warning" or "error"); default all
        group_by: "rule" (default: counts and the first findings per rule)
                  or "none" (one flat list)
        limit: Findings listed per rule (group_by "rule") or in all
               ("none"), 1 to 10000; default 5
        offset: Findings to skip first (per rule with "rule"); default 0
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    if rules is not None and (isinstance(rules, str) or not rules):
        raise ToolError("bad_request: rules must be a non-empty list of rule ids and/or set names")
    params = _with({"group_by": group_by, "limit": limit}, doc=doc,
                   rules=None if rules is None else list(rules), severity_min=severity_min,
                   offset=offset or None)
    result = client.call("lint", params, timeout_s=timeout_s)
    return _done(result, _lint_summary(result, doc))


@mcp.tool()
def krita_layer_stats(ids: list[str], buckets: Optional[list[int]] = None,
                      doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Pixel statistics of paint layers, read over each layer's bounds (exact
    and not clipped to the canvas). Reads only. The document must be 8-bit
    RGBA. ids are layer refs (UUID, name path, unique name); an unknown ref
    fails with not_found, and layers that aren't paint layers (groups,
    masks, filter layers, ...) are listed in skipped with a reason.

    Per layer: bounds (Krita's, {x, y, w, h} or null), opaque_bounds (the
    tight rectangle around alpha > 0, canvas coordinates), pixel_count
    (alpha > 0), opaque_count (alpha = 255), empty (no pixel with
    alpha > 0), alpha_histogram {edges, counts}, beyond_canvas, stray_count
    and stray_specks.
    - alpha_histogram: counts[i] is the number of pixels inside bounds with
      edges[i] <= alpha < edges[i+1]; the last bucket is edges[-1] <=
      alpha <= 255. With the default edges [0, 1, 8, 32, 128, 255]: alpha
      0 (transparent pixels inside bounds), 1-7, 8-31, 32-127, 128-254 and
      255. Alpha below edges[0] is in no bucket.
    - beyond_canvas: null when opaque_bounds is inside the canvas, else
      {left, top, right, bottom} (how many pixels opaque_bounds reaches
      past each canvas edge, 0 where it doesn't) and pixels (the number of
      alpha > 0 pixels outside the canvas).
    - stray_count / stray_specks: lint's stray-alpha numbers with the
      config's lint settings (returned as settings): faint pixels (alpha
      below stray_alpha_below) farther than stray_alpha_distance from any
      stronger pixel, and their isolated specks {count, pixels, regions,
      samples, truncated}.

    Returns {doc, canvas {w, h}, buckets, settings, layers, skipped:
    [{id, path, type, reason}]}.

    Args:
        ids: The layers, 1 to 2000 refs
        buckets: Histogram edges: 2 to 16 strictly increasing whole numbers
                 0-255; default [0, 1, 8, 32, 128, 255]
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = _with({"ids": list(ids)}, buckets=None if buckets is None else list(buckets),
                   doc=doc)
    result = client.call("layer_stats", params, timeout_s=timeout_s)
    layers = result.get("layers") or []
    skipped = result.get("skipped") or []
    notes = []
    for count, what in ((sum(1 for x in layers if x.get("empty")), "empty"),
                        (sum(1 for x in layers if x.get("beyond_canvas")), "past the canvas"),
                        (sum(1 for x in layers if (x.get("stray_specks") or {}).get("count")),
                         "with stray specks"),
                        (len(skipped), "skipped (not paint layers)")):
        if count:
            notes.append(f"{count} {what}")
    summary = ("Stats for " + _count(len(layers), "paint layer")
               + (_in(result) or f" in {doc or 'the active document'}")
               + (f" ({', '.join(notes)})" if notes else ""))
    return _done(result, summary)


@mcp.tool()
def krita_occlusion_map(id: str, path: str, modes: Optional[list[str]] = None,
                        threshold: Annotated[float, Field(gt=0, le=1)] = 0.5,
                        png_level: Annotated[Optional[int], Field(ge=0, le=9)] = None,
                        doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    How much of a paint layer is hidden by the layers drawn after it. Use it
    in part separation to find the areas of a part that are covered anyway
    and don't need painting (or cleaning up). Reads only: the coverage is
    rendered in a private copy of the document, closed afterwards.

    What counts as above: everything drawn after the layer in the global
    draw order (above it in its group, and above each of its ancestor
    groups at every level), visible with all its ancestors visible, and in
    one of modes (default only "normal": Multiply, Add, ... layers tint
    what is below rather than hide it; "add" and "linear_dodge" are the
    same mode). A paint layer counts by its own blend mode; an isolated
    group counts as one, by the group's mode, with everything visible in
    it; a pass-through group's children count one by one (its own mode is
    ignored, its opacity applies). Vector, fill, file, clone and filter
    layers don't count. Clipped (inherit alpha) layers that clip to a stack
    holding the target count with their own alpha. The coverage is the
    alpha of those layers composited (their opacity and masks included);
    the layer's own opacity, clipping and masks are ignored (its own
    pixels' alpha is used). Past the canvas edge nothing is rendered, so
    coverage there is 0 (off_canvas_px counts those pixels).

    Writes a grayscale PNG the size of the layer's bounds: value = layer
    alpha x coverage / 255 (0-255, exact), i.e. how much of each pixel is
    hidden. covered_pct is the share (0-1) of the layer's pixels with
    alpha > 0 whose coverage is at least threshold (0.5 is 128 of 255);
    fully_hidden is true when every such pixel is covered completely
    (coverage 255). An empty layer writes no file and returns path,
    bounds and covered_pct null. 8-bit RGBA documents only.

    Returns {doc, layer (id), name, path (resolved, or null), bounds,
    pixels (alpha > 0), covered_px, off_canvas_px, covered_pct,
    fully_hidden, above {count, ids (top first, when at most 50)}, modes,
    threshold, bytes, png_level when given}.

    Args:
        id: The paint layer: UUID, name path such as "Head/Eyes", or unique
            name (anything else fails with "conflict")
        path: PNG file to write (must end in .png): absolute, "~"-prefixed
              or relative to the output dir, inside an allowed directory;
              missing parent directories are created, an existing file is
              replaced
        modes: Blend-mode ids whose layers hide what is below, such as
               ["normal", "multiply"]; default ["normal"]
        threshold: Coverage a pixel needs to count as covered, greater
                   than 0 and at most 1 (default 0.5)
        png_level: zlib level 0 (fastest, biggest) to 9 (smallest), or
                   null for the default (6)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not 0 < threshold <= 1):
        raise ToolError(f"bad_request: threshold must be greater than 0 and at most 1, "
                        f"got {threshold!r}")
    if modes is not None and (isinstance(modes, str) or not modes):
        raise ToolError("bad_request: modes must be a non-empty list of blend-mode ids")
    params = _with({"id": id, "path": path}, modes=None if modes is None else list(modes),
                   threshold=None if threshold == 0.5 else threshold, png_level=png_level,
                   doc=doc)
    result = client.call("occlusion_map", params, timeout_s=timeout_s)
    name = result.get("name") or id
    above = (result.get("above") or {}).get("count", 0)
    by = f"by {_count(above, 'layer')} above"
    if result.get("covered_pct") is None:
        summary = f"'{name}'{_in(result)} is empty: nothing to map ({_count(above, 'layer')} above)"
    elif result.get("fully_hidden"):
        summary = f"'{name}'{_in(result)} is fully hidden {by}"
    else:
        summary = (f"'{name}'{_in(result)} is {result['covered_pct'] * 100:.1f}% covered "
                   f"(coverage >= {result.get('threshold', threshold):g}) {by}")
    if result.get("path"):
        b = result.get("bounds") or {}
        summary += f"; map {b.get('w')}x{b.get('h')} written to {result['path']}"
    if result.get("off_canvas_px"):
        summary += f" ({result['off_canvas_px']} px past the canvas, uncovered)"
    return _done(result, summary)


def _diff_summary(result: dict, doc_a: str, doc_b: str) -> str:
    a, b = result.get("doc_a") or doc_a, result.get("doc_b") or doc_b
    counts = result.get("counts") or {}
    parts = [f"{counts.get(k, 0)} {k}" for k in ("added", "removed", "renamed", "moved", "changed")
             if counts.get(k)]
    pixel_changes = counts.get("pixel_changes")
    if pixel_changes:
        parts.append(f"{_count(pixel_changes, 'layer')} with pixel changes")
    projection = result.get("projection") or {}
    if result.get("projection_changed"):
        text = "projection changed"
        if projection.get("changed_count"):
            box = projection.get("bounds") or {}
            text += (f": {_count(projection['changed_count'], 'visible pixel')}, max channel "
                     f"difference {projection.get('max_channel_diff')}, within "
                     f"{_rect_text(box)}")
        parts.append(text)
    alpha0 = projection.get("alpha0_rgb_count")
    alpha0_layers = counts.get("alpha0_rgb_only_layers")
    hidden = []
    if alpha0:
        hidden.append(f"{_count(alpha0, 'transparent pixel')} of the projection")
    if alpha0_layers:
        hidden.append(_count(alpha0_layers, "layer"))
    if hidden:
        verb = "ignored" if result.get("ignore_alpha0_rgb", True) else "counted"
        parts.append("colour under alpha 0 differs in " + " and ".join(hidden) + f" ({verb})")
    matched = counts.get("matched") or {}
    how = ", ".join(f"{matched[k]} by {k}" for k in ("uuid", "path", "name") if matched.get(k))
    note = f" ({_count(matched.get('total', 0), 'layer')} matched{': ' + how if how else ''})"
    if parts:
        summary = f"{a} -> {b}: " + ", ".join(parts) + note
    else:
        summary = f"No differences between {a} and {b}" + note
    if result.get("document_changes"):
        summary += "; document: " + ", ".join(
            f"{k} {v[0]} -> {v[1]}" for k, v in result["document_changes"].items()
            if isinstance(v, (list, tuple)) and len(v) == 2)
    return summary + _warnings_note(result.get("warnings"))


@mcp.tool()
def krita_diff_documents(doc_a: str, doc_b: str, pixels: bool = True,
                         ignore_alpha0_rgb: bool = True,
                         timeout_s: Optional[float] = None) -> dict:
    """
    Compare two open documents: which layers were added, removed, renamed,
    moved or changed, and (with pixels) which paint layers' pixels differ.
    Reads only. Use it to check a rebuilt or re-imported document against
    the original, or what an edit changed (diff a saved copy against the
    working document).

    Layers are matched in three passes, each among the layers still
    unmatched: by id (the same UUID in both: a .kra save and reopen keeps
    ids, a PSD / ORA import or a rebuild from a manifest doesn't), then by
    name path ("Head/Eye L", when exactly one layer in each document has
    it), then by name (when the name is unique in each whole document).
    Each reported pair says how it was matched ("match": "uuid", "path" or
    "name"). Layers with a repeated path and a repeated name can't be
    matched and show up as removed + added.

    - added / removed: [{id, name, path, type}] only in doc_b / only in doc_a
    - renamed: [{id_a, id_b, path (in doc_b), path_a (when it differs),
      match, name: [a, b]}]
    - moved: parent or index changed (index counts from the bottom within
      the parent, so adding or deleting a layer below a sibling moves it
      too): [{..., parent: [id_a, id_b] (null at the top level),
      index: [a, b], parent_changed}]
    - changed: property deltas, [{..., fields: {name: [a, b]}}] over type,
      visible, opacity, blend_mode, locked, alpha_locked, inherit_alpha,
      pass_through, has_layer_style
    - pixel_changes (with pixels): [{..., changed_count, bounds {x, y, w,
      h}, max_channel_diff, alpha0_rgb_count, alpha0_rgb_bounds}] for
      matched paint layers whose pixels differ (over both layers' bounds,
      grown to Krita's 64 px tiles). changed_count counts the visible
      changes (alpha > 0 in either), bounds is their box and
      max_channel_diff the largest difference in any channel (0-255; 1
      means rounding). alpha0_rgb_count counts the pixels transparent in
      both whose hidden colour differs (no visible effect). Null when not
      compared: pixels=false, documents of different sizes, or not 8-bit
      RGBA (a warning says which)
    - projection_changed: whether the composited images differ (null when
      pixels were not compared); projection: the same numbers as a
      pixel_changes entry for the whole composite, so a change can be
      judged by size and place without rendering
    - ignore_alpha0_rgb (default true): differences only in the colour of
      fully transparent pixels don't make projection_changed true or put a
      layer in pixel_changes (they are still counted: projection's
      alpha0_rgb_count, counts.alpha0_rgb_only_layers)
    - document_changes: {width | height | color_model | color_depth |
      color_profile | resolution: [a, b]} for the ones that differ
    - counts: per category, plus matched {uuid, path, name, total} and
      alpha0_rgb_only_layers

    Args:
        doc_a: The first (older) document: handle such as "doc-3", or the
               absolute path of an open document
        doc_b: The second (newer) document, a different one
        pixels: Compare paint layers' pixels and the projections too
                (default true; false for a quick structural diff)
        ignore_alpha0_rgb: Treat pixels that are transparent in both
                           documents as equal whatever colour they hide
                           (default true; false counts them as changes)
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    params = {"doc_a": doc_a, "doc_b": doc_b}
    if not pixels:
        params["pixels"] = False
    if not ignore_alpha0_rgb:
        params["ignore_alpha0_rgb"] = False
    result = client.call("diff_documents", params, timeout_s=timeout_s)
    return _done(result, _diff_summary(result, doc_a, doc_b))


@mcp.tool()
def krita_set_color(color: str, timeout_s: Optional[float] = None) -> dict:
    """
    Set the foreground (paint) color used by stroke, fill and draw_shape.
    The plugin stores it, so it works without a Krita window and applies to
    every document; when a window is open its foreground color is set too
    (``view_updated``).

    Args:
        color: Hex color code (e.g., "#ff6b6b", "#b8a9c9")
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("set_color", {"color": color}, timeout_s=timeout_s)
    note = " (stored; no Krita window to update)" if result.get("view_updated") is False else ""
    return _done(result, f"Color set to {result.get('color', color)}{note}")


@mcp.tool()
def krita_set_brush(
    preset: Optional[str] = None,
    size: Optional[int] = None,
    opacity: Optional[float] = None,
    timeout_s: Optional[float] = None,
) -> dict:
    """
    Set brush preset and properties. Size and opacity are stored by the
    plugin (used by stroke) and work without a Krita window; a preset needs
    one (conflict otherwise). ``view_updated`` says whether the window's
    brush changed.

    Args:
        preset: Brush preset name (partial match, e.g., "Basic", "Soft", "Airbrush")
        size: Brush size in pixels (1 to 1000)
        opacity: Brush opacity (0.0 to 1.0)
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    params = {}
    if preset:
        params["preset"] = preset
    if size is not None:
        params["size"] = size
    if opacity is not None:
        params["opacity"] = opacity

    result = client.call("set_brush", params, timeout_s=timeout_s)
    changed = [f"{k}={result.get(k, v)}" for k, v in params.items()]
    return _done(result, "Brush set: " + ", ".join(changed) if changed else "Brush unchanged")


@mcp.tool()
def krita_stroke(points: list[list[int]], pressure: float = 1.0,
                 color: Optional[str] = None,
                 doc: Optional[str] = None, layer: Optional[str] = None,
                 timeout_s: Optional[float] = None) -> dict:
    """
    Paint a stroke through a series of points with the current color, or
    with ``color``.

    One call is one agent undo step (krita_undo restores the old pixels;
    a write too big for the undo journal's caps warns "not_undoable").

    Args:
        points: List of [x, y] coordinate pairs, e.g., [[100, 100], [150, 120], [200, 150]]
        pressure: Brush pressure (0.0 to 1.0, affects stroke thickness/opacity)
        color: Paint color for this call only, as hex (e.g. "#3fa34d");
               default the current color (krita_set_color), which this
               does not change
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        layer: Paint layer to paint into: its id (UUID), a name path from
               the root such as "Head/Eyes" (write \\/ for a "/" in a name),
               or a unique name; default the document's active layer. Groups
               and masks fail with conflict
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    if len(points) < 2:
        raise ToolError("bad_request: Need at least 2 points for a stroke")

    result = client.call("stroke", _with({
        "points": points,
        "pressure": pressure
    }, color=color, doc=doc, layer=layer), timeout_s=timeout_s)
    return _done(result, f"Stroke painted with {result.get('points_count', len(points))} points"
                         + _in(result) + _warnings_note(result.get("warnings")))


@mcp.tool()
def krita_fill(x: int, y: int, radius: int = 50, color: Optional[str] = None,
               doc: Optional[str] = None, layer: Optional[str] = None,
               timeout_s: Optional[float] = None) -> dict:
    """
    Fill an area with the current color, or with ``color`` (paints a
    filled circle at the point).

    One call is one agent undo step (krita_undo restores the old pixels;
    a write too big for the undo journal's caps warns "not_undoable").

    Args:
        x: X coordinate
        y: Y coordinate
        radius: Fill radius in pixels
        color: Paint color for this call only, as hex (e.g. "#3fa34d");
               default the current color (krita_set_color), which this
               does not change
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        layer: Paint layer to paint into: its id (UUID), a name path from
               the root such as "Head/Eyes" (write \\/ for a "/" in a name),
               or a unique name; default the document's active layer. Groups
               and masks fail with conflict
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("fill", _with({"x": x, "y": y, "radius": radius}, color=color, doc=doc,
                                       layer=layer), timeout_s=timeout_s)
    return _done(result, f"Filled circle at ({x}, {y}) with radius {radius}" + _in(result)
                 + _warnings_note(result.get("warnings")))


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
    y2: Optional[int] = None,
    color: Optional[str] = None,
    doc: Optional[str] = None,
    layer: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> dict:
    """
    Draw a shape on the canvas with the current color, or with ``color``
    (no krita_set_color call needed).

    One call is one agent undo step (krita_undo restores the old pixels;
    a write too big for the undo journal's caps warns "not_undoable").

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
        color: Paint color for this call only, as hex (e.g. "#3fa34d");
               default the current color (krita_set_color), which this
               does not change
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        layer: Paint layer to paint into: its id (UUID), a name path from
               the root such as "Head/Eyes" (write \\/ for a "/" in a name),
               or a unique name; default the document's active layer. Groups
               and masks fail with conflict
        timeout_s: Seconds to wait for Krita, up to 900; default per command
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

    result = client.call("draw_shape", _with(params, color=color, doc=doc, layer=layer),
                         timeout_s=timeout_s)
    return _done(result, f"Drew {result.get('shape', shape)} at ({x}, {y})" + _in(result)
                 + _warnings_note(result.get("warnings")))


@mcp.tool()
def krita_get_canvas(filename: str = "canvas.png", doc: Optional[str] = None,
                     timeout_s: Optional[float] = None) -> dict:
    """
    Save a document's whole canvas as a PNG in the output directory and
    return the path; use this to see your painting progress. A shortcut for
    krita_render(path=<output dir>/<filename>) of the full canvas at scale
    1: the document is not changed. For a smaller, faster preview, a
    region, or some layers only, use krita_render (e.g. scale=0.25).

    Returns {doc, path, width, height, region, scale, bytes}.

    Args:
        filename: Output file name, saved in the output directory (any
                  directory part is dropped; ".png" is added if missing)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    result = client.call("get_canvas", _with({"filename": filename}, doc=doc),
                         timeout_s=timeout_s)
    return _done(result, _render_summary(result, filename, what="Saved the canvas of {doc}",
                                         what_no_doc="Saved the canvas", via_render=True))


def _journal_summary(result: dict, verb: str, done_key: str) -> str:
    done = result.get(done_key) or {}
    remaining = result.get("remaining") or {}
    layers = done.get("layers") or []
    summary = (f"{verb} {done.get('label', 'the last agent action')}"
               + (f" ({_count(len(layers), 'layer')})" if layers else "")
               + _in(result)
               + f"; {remaining.get('undo', 0)} to undo, {remaining.get('redo', 0)} to redo"
               + _warnings_note(result.get("warnings")))
    return summary


@mcp.tool()
def krita_undo(doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Undo the newest agent tool call on a document: one call reverts one
    whole tool call (a create_layers batch, an update, a move, a delete,
    an import_png, a stroke, fill, draw_shape or clear), restoring layer
    ids, properties, order and pixels exactly.

    This is the plugin's own agent journal, not Krita's Edit > Undo. It
    only knows agent edits: edits made by hand in Krita's UI aren't
    recorded, so undoing an agent action after painting by hand on the
    same layer overwrites those strokes in that rectangle. An entry whose
    layers were deleted (or moved out of reach) by hand is refused with
    "conflict" and stays on the stack; nothing is changed. Empty journal:
    "conflict". The journal keeps the last 20 calls (configurable) per
    document and is dropped when the document closes. For Krita's own
    undo stack, use krita_ui_undo.

    Returns {doc, undone: {label (the tool), layers (ids touched)},
    remaining: {undo, redo}, warnings}.

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    result = client.call("undo", _with({}, doc=doc), timeout_s=timeout_s)
    return _done(result, _journal_summary(result, "Undid", "undone"))


@mcp.tool()
def krita_redo(doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Redo the newest agent tool call undone with krita_undo on a document,
    exactly as it was. Any new agent edit to the document clears the redo
    stack. Like krita_undo it only knows agent edits, refuses entries whose
    layers are gone ("conflict", nothing changed), and fails with
    "conflict" when there is nothing to redo. For Krita's own redo, use
    krita_ui_redo.

    Returns {doc, redone: {label, layers}, remaining: {undo, redo},
    warnings}.

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    result = client.call("redo", _with({}, doc=doc), timeout_s=timeout_s)
    return _done(result, _journal_summary(result, "Redid", "redone"))


@mcp.tool()
def krita_journal(doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    List a document's agent undo journal (read-only): what krita_undo and
    krita_redo would revert or re-apply, next first.

    Returns {doc, undo: [{label, layer_count, nbytes, disk_bytes,
    on_disk}], redo: [same], memory_bytes, disk_bytes, limits:
    {max_actions, max_bytes, max_disk_bytes}}. Pixel snapshots beyond the
    memory cap are kept in temporary files (on_disk).

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("journal", _with({}, doc=doc), timeout_s=timeout_s)
    undo, redo = result.get("undo") or [], result.get("redo") or []
    summary = f"Agent journal{_in(result)}: {len(undo)} to undo"
    if undo:
        summary += f" (next: {undo[0].get('label')})"
    summary += f", {len(redo)} to redo"
    if redo:
        summary += f" (next: {redo[0].get('label')})"
    return _done(result, summary)


@mcp.tool()
def krita_ui_undo(doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Krita's own Edit > Undo on the document's view: it acts on Krita's
    undo stack, which only partly covers agent edits. Property changes,
    pixel writes (import_png and the painting tools) and moves made by
    agent tools are not on it, so this may skip them or revert an
    unrelated earlier step; to revert agent tool calls use krita_undo.
    It is for edits made by hand in the UI, and doesn't change the agent
    journal.

    With doc, that document's view is made the active one first; a
    document with no view in a Krita window (always so without a GUI)
    fails with "conflict". Without doc, the active view.

    Returns {doc, action, activated_view, warnings}; the warning repeats
    the caveat above.

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("ui_undo", _with({}, doc=doc), timeout_s=timeout_s)
    return _done(result, "Triggered Krita's Edit > Undo" + _in(result)
                 + _warnings_note(result.get("warnings")))


@mcp.tool()
def krita_ui_redo(doc: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Krita's own Edit > Redo on the document's view: it acts on Krita's
    undo stack, which only partly covers agent edits. Property changes,
    pixel writes (import_png and the painting tools) and moves made by
    agent tools are not on it, so this may skip them or revert an
    unrelated earlier step; to revert agent tool calls use krita_redo.
    It is for edits made by hand in the UI, and doesn't change the agent
    journal.

    With doc, that document's view is made the active one first; a
    document with no view in a Krita window (always so without a GUI)
    fails with "conflict". Without doc, the active view.

    Returns {doc, action, activated_view, warnings}; the warning repeats
    the caveat above.

    Args:
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("ui_redo", _with({}, doc=doc), timeout_s=timeout_s)
    return _done(result, "Triggered Krita's Edit > Redo" + _in(result)
                 + _warnings_note(result.get("warnings")))


@mcp.tool()
def krita_clear(color: str = "#1a1a2e", doc: Optional[str] = None,
                layer: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Clear a paint layer to a solid color over the whole canvas, or erase it.

    One call is one agent undo step (krita_undo restores the old pixels;
    a write too big for the undo journal's caps warns "not_undoable").

    Args:
        color: Color to fill the layer's canvas area with (default dark
               blue), or "transparent" to erase the layer completely,
               including pixels past the canvas edges
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        layer: Paint layer to paint into: its id (UUID), a name path from
               the root such as "Head/Eyes" (write \\/ for a "/" in a name),
               or a unique name; default the document's active layer. Groups
               and masks fail with conflict
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("clear", _with({"color": color}, doc=doc, layer=layer),
                         timeout_s=timeout_s)
    return _done(result, f"Layer{_in(result)} cleared to {result.get('color', color)}"
                 + _warnings_note(result.get("warnings")))


@mcp.tool()
def krita_get_color_at(x: int, y: int, doc: Optional[str] = None, layer: Optional[str] = None,
                       timeout_s: Optional[float] = None) -> dict:
    """
    Sample the color at a specific pixel (eyedropper). The point must be
    inside the document (0 <= x < width, 0 <= y < height).

    Args:
        x: X coordinate
        y: Y coordinate
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        layer: Sample one layer instead of the composite: its id (UUID), a
               name path from the root such as "Head/Eyes" (write \\/ for
               a "/" in a name), or a unique name. A paint layer gives its
               own pixel, a group its composite; other types fail with
               conflict. Default: the whole image's composite
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("get_color_at", _with({"x": x, "y": y}, doc=doc, layer=layer),
                         timeout_s=timeout_s)
    return _done(result, f"Color at ({x}, {y}){_in(result)}: {result.get('color', 'unknown')} "
                         f"(R:{result.get('r')}, G:{result.get('g')}, B:{result.get('b')}, "
                         f"A:{result.get('a')})")


@mcp.tool()
def krita_list_brushes(filter: str = "", limit: int = 20,
                       timeout_s: Optional[float] = None) -> dict:
    """
    List available brush presets.

    Args:
        filter: Filter brushes by name (partial match)
        limit: Maximum number to return
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("list_brushes", {"filter": filter, "limit": limit},
                         timeout_s=timeout_s)
    brushes = result.get("brushes", [])
    if not brushes:
        summary = "No brushes found matching filter" + (f" '{filter}'" if filter else "")
    else:
        summary = f"Found {len(brushes)} brushes" + (f" matching '{filter}'" if filter else "")
    return _done(result, summary)


if __name__ == "__main__":
    mcp.run()
