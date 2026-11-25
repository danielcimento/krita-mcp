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
from krita_mcp_server.models import LayerChange, LayerSpec, Move  # noqa: E402

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
    directories file tools may use (the first is the output dir), and
    ``config_errors`` the problems with the plugin's config file
    (``~/.krita-mcp-config.json``), such as a skipped directory. ``busy``
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
        summary += f"; {len(allowed_dirs)} allowed dir{'s' if len(allowed_dirs) != 1 else ''}"
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
    (``background_layer`` is its id). The defaults are kept from earlier
    versions for compatibility.

    Args:
        width: Canvas width in pixels (default 800)
        height: Canvas height in pixels (default 600)
        name: Document name (default "New Canvas")
        background: Background color as hex (default "#1a1a2e", dark blue),
                    or "transparent" for an empty Background layer
        color_profile: An installed RGBA/U8 profile name; default Krita's
                       sRGB ("sRGB-elle-V2-srgbtrc.icc"). An unknown name
                       fails with bad_request listing the available ones
        resolution: Resolution in dpi, 1 to 10000 (default 120)
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


@mcp.tool()
def krita_list_layers(doc: Optional[str] = None, root: Optional[str] = None,
                      depth: Optional[int] = None, fields: Optional[list[str]] = None,
                      timeout_s: Optional[float] = None) -> dict:
    """
    List a document's layer tree as a flat list of layer objects,
    depth-first and top to bottom (the order of Krita's Layers docker: a
    group comes right before its contents). Returns {doc, root, layers,
    counts}: root is "/" or the root layer's id; counts has nodes per type
    over the listed layers (e.g. {"grouplayer": 38, "paintlayer": 184}) and
    "total". The document's global selection is never listed.

    Each layer object has: id (braced UUID), name, path (name path from the
    document root, "/" in names escaped as "\\/"), type ("paintlayer",
    "grouplayer", ...), parent (id, null at the top level), index (position
    in the parent counted from the bottom, like Krita's childNodes()),
    visible (the layer's own flag), opacity (0.0-1.0), blend_mode (a Krita
    id such as "normal", "multiply", "add"), locked, alpha_locked,
    inherit_alpha (clipping), pass_through (bool on groups, null
    otherwise), has_layer_style, bounds and children (child ids, top to
    bottom). bounds is {x, y, w, h} in exact canvas pixels, or null for a
    layer with no pixels. It is not clipped to the canvas, so it may start
    at negative x/y or extend past the canvas edge.

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
        fields: Layer-object keys to include, e.g. ["type", "bounds"], to keep
                big listings small; id, name and children are always
                included. Unknown keys fail with bad_request. Default all
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("list_layers",
                         _with({}, doc=doc, root=root, depth=depth, fields=fields),
                         timeout_s=timeout_s)
    layers = result.get("layers") or []
    counts = result.get("counts") or {}
    what = _type_counts(counts) or "No layers"
    if result.get("root") not in (None, "/") and layers:
        first = layers[0]
        what += f" under '{first.get('path') or first.get('name')}'"
    summary = what + (_in(result) or f" in {doc or 'the active document'}")
    if depth is not None:
        summary += f" (depth {depth})"
    return _done(result, summary)


@mcp.tool()
def krita_create_layers(specs: Optional[list[LayerSpec]] = None, manifest: Optional[str] = None,
                        doc: Optional[str] = None, dry_run: bool = False,
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
    (default 0, 0; only with png). Pixels outside the canvas are kept, not
    clipped. Unknown keys fail.

    A manifest is the manifest.json that krita_export_layers writes (or
    tools/ora_to_manifest.py): {"version": 1, "document": {...}, "layers":
    [...]}, its layers in create order, each with a unique "key". Parents
    and "above" are "$key" refs only. Each "png" is relative to the
    manifest's directory and must still resolve inside an allowed
    directory. Entries marked "projection": true are skipped. Errors name
    the manifest entry: "layers[i]: ..." with details.index = i.

    Returns {doc, created: [{key, id, name, type, path}], counts} (counts
    per Krita type plus "total"); with dry_run, {doc, would_create: [{key,
    name, type, parent, above, png?, x?, y?, w?, h?}], counts} and nothing
    changes. With a manifest it also has manifest (resolved path), skipped
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
        dry_run: Only validate and report what would be created
        timeout_s: Seconds to wait for Krita, up to 900; default 300
    """
    if (specs is None) == (manifest is None):
        raise ToolError("bad_request: pass exactly one of specs and manifest")
    if manifest is not None:
        params = {"manifest": manifest}
    else:
        params = {"specs": [(s if isinstance(s, LayerSpec) else LayerSpec.model_validate(s))
                            .to_params() for s in specs]}
    result = client.call("create_layers", _with(params, doc=doc, dry_run=dry_run or None),
                         timeout_s=timeout_s)
    counts = result.get("counts") or {}
    total = counts.get("total", len(result.get("created") or result.get("would_create") or []))
    groups = counts.get("grouplayer", 0)
    what = f"{total} layer{'s' if total != 1 else ''}"
    if groups:
        what += f" ({groups} group{'s' if groups != 1 else ''})"
    where = _in(result) or f" in {doc or 'the active document'}"
    verb = "Would create " if result.get("dry_run") else "Created "
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
    fails the call with details.index. A rename to a name another layer
    has is allowed but listed in warnings ({code: "duplicate_name", id,
    name, others}): a bare-name ref to it becomes ambiguous.

    Returns {doc, updated: [{id, path, changed: [fields set]}], warnings};
    with dry_run, {doc, would_update: [{id, path, set: {field: value}}],
    warnings} and nothing changes. One successful call is one agent undo
    step.

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
    changes. One successful call is one agent undo step, which puts the
    layers back with the same ids, pixels and properties (the deleted
    layers are kept in memory for that; a delete too big for the undo
    journal's byte cap warns "not_undoable").

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
                 doc: Optional[str] = None, layer: Optional[str] = None,
                 timeout_s: Optional[float] = None) -> dict:
    """
    Paint a stroke through a series of points with the current color.

    Args:
        points: List of [x, y] coordinate pairs, e.g., [[100, 100], [150, 120], [200, 150]]
        pressure: Brush pressure (0.0 to 1.0, affects stroke thickness/opacity)
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
    }, doc=doc, layer=layer), timeout_s=timeout_s)
    return _done(result, f"Stroke painted with {result.get('points_count', len(points))} points"
                         + _in(result))


@mcp.tool()
def krita_fill(x: int, y: int, radius: int = 50, doc: Optional[str] = None,
               layer: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Fill an area with current color (paints a filled circle at the point).

    Args:
        x: X coordinate
        y: Y coordinate
        radius: Fill radius in pixels
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        layer: Paint layer to paint into: its id (UUID), a name path from
               the root such as "Head/Eyes" (write \\/ for a "/" in a name),
               or a unique name; default the document's active layer. Groups
               and masks fail with conflict
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("fill", _with({"x": x, "y": y, "radius": radius}, doc=doc, layer=layer),
                         timeout_s=timeout_s)
    return _done(result, f"Filled circle at ({x}, {y}) with radius {radius}" + _in(result))


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
    doc: Optional[str] = None,
    layer: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> dict:
    """
    Draw a shape on the canvas with the current color.

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

    result = client.call("draw_shape", _with(params, doc=doc, layer=layer), timeout_s=timeout_s)
    return _done(result, f"Drew {result.get('shape', shape)} at ({x}, {y})" + _in(result))


@mcp.tool()
def krita_get_canvas(filename: str = "canvas.png", doc: Optional[str] = None,
                     timeout_s: Optional[float] = None) -> dict:
    """
    Export a document's canvas to a PNG file and return the path.
    Use this to see your painting progress.

    Args:
        filename: Output filename (saved to configured output directory)
        doc: Document handle such as "doc-3" (new_canvas and open_file return
             one), or the absolute path of an open document; default the
             active document
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("get_canvas", _with({"filename": filename}, doc=doc),
                         timeout_s=timeout_s)
    return _done(result, f"Canvas{_in(result)} saved to: {result.get('path', '')}")


@mcp.tool()
def krita_undo(timeout_s: Optional[float] = None) -> dict:
    """
    Undo the last action.

    Args:
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("undo", {}, timeout_s=timeout_s)
    return _done(result, "Undone")


@mcp.tool()
def krita_redo(timeout_s: Optional[float] = None) -> dict:
    """
    Redo the last undone action.

    Args:
        timeout_s: Seconds to wait for Krita, up to 900; default per command
    """
    result = client.call("redo", {}, timeout_s=timeout_s)
    return _done(result, "Redone")


@mcp.tool()
def krita_clear(color: str = "#1a1a2e", doc: Optional[str] = None,
                layer: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
    """
    Clear a paint layer to a solid color over the whole canvas, or erase it.

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
    return _done(result, f"Layer{_in(result)} cleared to {result.get('color', color)}")


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
