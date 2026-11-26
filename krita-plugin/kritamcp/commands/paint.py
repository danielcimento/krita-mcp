"""Painting commands: color, brush, stroke, fill, shapes, clear, eyedropper.

Painting tools take ``doc`` (D3) and ``layer`` (D4, default the document's
active node) and need no view. The foreground color and brush size and
opacity are kept here, plugin-side (D15), and mirrored to the active view
when there is one.

Each painting call (stroke, fill, draw_shape, clear) reads the rectangle it
writes before writing it and pushes that snapshot onto the document's agent
journal as a ``RestorePixels`` (D6), so ``krita_undo`` reverts one call.
"""

from krita import Krita, ManagedColor
from PyQt5.QtCore import QRect
from PyQt5.QtGui import QColor

from .. import batch, nodes, registry
from .. import pixels as pixelbuf
from ..config import COORD, MAX_BRUSH_SIZE, MAX_LIMIT, MAX_STROKE_POINTS
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from ..params import float_param, int_param
from ._active import get_active_view
from .pixels import RestorePixels

# Brush state, set by set_brush and used as the stroke defaults.
current_brush_size = 20
current_opacity = 1.0
# The foreground color as "#rrggbb", or None until set_color (D15).
current_color = None

# set_color and set_brush change plugin and UI state, not a document, so
# they are registered as non-mutating.


def _color_param(params, key="color", default=None):
    value = params.get(key, default)
    if not isinstance(value, str) or not QColor(value).isValid():
        raise CommandError("bad_request",
                           f"'{key}' must be a color such as '#ff6b6b', got {value!r}")
    return QColor(value)


def _paint_rgb(params=None):
    """The paint color: ``params["color"]`` when given (this call only; the
    stored color is unchanged, F003 K7); else the stored one; else the
    active view's foreground; else black."""
    if params is not None and params.get("color") is not None:
        color = _color_param(params)
        return color.red(), color.green(), color.blue()
    if current_color is not None:
        color = QColor(current_color)
    else:
        view = get_active_view()
        if view is None:
            return 0, 0, 0
        color = view.foregroundColor().colorForCanvas(view.canvas())
    return color.red(), color.green(), color.blue()


def _paint_target(params):
    """``(handle, doc, node)``: the document and paint layer to paint into.

    ``layer`` absent: the document's active node. Anything but a paint
    layer (a group, a mask, …) is ``conflict``.
    """
    handle, doc = registry.resolve_doc(params)
    ref = params.get("layer")
    if ref is None:
        node = nodes.active_node(doc)
        if node is None:
            raise CommandError("not_found", f"No active layer in {handle}; pass 'layer'",
                               {"doc": handle})
    else:
        node = nodes.resolve_node(doc, ref)
    kind = node.type()
    if kind != "paintlayer":
        nid = nodes.node_id(node)
        raise CommandError(
            "conflict",
            f"Layer '{node.name()}' is a {kind}, not a paint layer; pass a paint layer as 'layer'",
            {"doc": handle, "layer": nid, "type": kind})
    return handle, doc, node


def _record(handle, label, layer, rect, old):
    """Push one journal entry that writes ``old`` (the layer's bytes over
    ``rect`` read before the call wrote it) back. Returns the result's extra
    fields: ``{}``, or ``{"warnings": [not_undoable]}`` when the snapshot
    is over the journal's caps (the journal is then cleared)."""
    inverse = RestorePixels(nodes.node_id(layer), [(rect, old)])
    if batch.JOURNALS.get(handle).push(label, [inverse], inverse.nbytes):
        return {}
    return {"warnings": [{
        "code": "not_undoable",
        "message": f"The old pixels take {inverse.nbytes} bytes, over the undo journal's memory "
                   f"and disk caps: this {label} can't be undone, and the agent undo journal "
                   "was cleared",
        "nbytes": inverse.nbytes}]}


@command("set_color")
def cmd_set_color(params):
    """Set the foreground color: stored plugin-side, and set on the active
    view too when there is one."""
    global current_color
    color = _color_param(params)
    current_color = color.name()
    view = get_active_view()
    if view is not None:
        view.setForeGroundColor(ManagedColor.fromQColor(color, view.canvas()))
    return {"color": current_color, "view_updated": view is not None}


@command("set_brush")
def cmd_set_brush(params):
    """Set the brush preset (needs a view), size and opacity (stored)."""
    global current_brush_size, current_opacity
    preset_name = params.get("preset", None)
    size = params.get("size", None)
    opacity = params.get("opacity", None)

    if size is not None:
        size = int_param(params, "size", None, 1, MAX_BRUSH_SIZE)
    if opacity is not None:
        opacity = float_param(params, "opacity", None, 0.0, 1.0)

    view = get_active_view()
    found = None
    if preset_name:
        if not isinstance(preset_name, str):
            raise CommandError("bad_request", "'preset' must be a string")
        if view is None:
            raise CommandError("conflict",
                               "No active view: a brush preset can only be set in a Krita window")
        for name, preset in Krita.instance().resources("preset").items():
            if preset_name.lower() in name.lower():
                found = (name, preset)
                break
        if found is None:
            raise CommandError("not_found", f"Brush preset not found: {preset_name}")

    view_updated = False
    if found is not None:
        view.setCurrentBrushPreset(found[1])
        view_updated = True
    if size is not None:
        current_brush_size = size
        if view is not None:
            view.setBrushSize(size)
            view_updated = True
    if opacity is not None:
        current_opacity = opacity  # used per stroke; not set on the view

    return {"preset": found[0] if found else None, "size": current_brush_size,
            "opacity": current_opacity, "view_updated": view_updated}


@command("stroke", mutating=True)
def cmd_stroke(params):
    """Paint a stroke along points using pixel-level drawing with soft edges."""
    points = params.get("points", [])
    brush_size = int_param(params, "size", current_brush_size, 1, MAX_BRUSH_SIZE)
    hardness = float_param(params, "hardness", 0.5, 0.0, 1.0)  # 0.0 = very soft, 1.0 = hard edge
    opacity = float_param(params, "opacity", current_opacity, 0.0, 1.0)

    if not isinstance(points, list) or len(points) < 2:
        raise CommandError("bad_request", "Need at least 2 points for a stroke")
    if len(points) > MAX_STROKE_POINTS:
        raise CommandError("bad_request", f"Too many points (max {MAX_STROKE_POINTS})")
    for p in points:
        if (not isinstance(p, list) or len(p) != 2
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                           and -COORD <= c <= COORD for c in p)):
            raise CommandError("bad_request", "Each point must be an [x, y] pair of numbers")

    handle, doc, layer = _paint_target(params)
    r, g, b = _paint_rgb(params)

    width = doc.width()
    height = doc.height()
    radius = max(1, brush_size // 2)

    # Calculate bounding box for all points plus brush radius
    min_x = max(0, int(min(p[0] for p in points)) - radius - 2)
    min_y = max(0, int(min(p[1] for p in points)) - radius - 2)
    max_x = min(width, int(max(p[0] for p in points)) + radius + 2)
    max_y = min(height, int(max(p[1] for p in points)) + radius + 2)

    w = max_x - min_x
    h = max_y - min_y

    if w <= 0 or h <= 0:
        raise CommandError("bad_request", "Stroke out of bounds")

    # Get existing pixel data for the affected region (also the undo snapshot)
    old = pixelbuf.as_bytes(layer.pixelData(min_x, min_y, w, h))
    pixels = bytearray(old)

    import math

    def draw_soft_circle(cx, cy, point_opacity=1.0):
        """Draw a soft circle with falloff at canvas coordinates."""
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                dist_sq = dx*dx + dy*dy
                if dist_sq <= radius*radius:
                    px = int(cx) + dx - min_x
                    py = int(cy) + dy - min_y
                    if 0 <= px < w and 0 <= py < h:
                        # Calculate distance from center (0.0 to 1.0)
                        dist = math.sqrt(dist_sq) / radius if radius > 0 else 0

                        # Apply hardness curve
                        # hardness=1.0: sharp edge, hardness=0.0: gradual fade from center
                        if hardness >= 1.0:
                            alpha_factor = 1.0
                        else:
                            # Soft falloff: starts fading at hardness point
                            if dist < hardness:
                                alpha_factor = 1.0
                            else:
                                # Smooth falloff from hardness to edge
                                falloff = (dist - hardness) / (1.0 - hardness) if hardness < 1.0 else 0
                                alpha_factor = 1.0 - falloff

                        final_alpha = int(255 * alpha_factor * opacity * point_opacity)

                        if final_alpha > 0:
                            idx = (py * w + px) * 4
                            # Alpha blending with existing pixel
                            existing_b = pixels[idx]
                            existing_g = pixels[idx+1]
                            existing_r = pixels[idx+2]
                            existing_a = pixels[idx+3]

                            # Simple alpha blend
                            blend = final_alpha / 255.0
                            new_r = int(existing_r * (1 - blend) + r * blend)
                            new_g = int(existing_g * (1 - blend) + g * blend)
                            new_b = int(existing_b * (1 - blend) + b * blend)
                            new_a = max(existing_a, final_alpha)

                            pixels[idx] = new_b
                            pixels[idx+1] = new_g
                            pixels[idx+2] = new_r
                            pixels[idx+3] = new_a

    def draw_line(x1, y1, x2, y2):
        """Draw a line using interpolation with soft brush circles."""
        dist = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        # More steps for smoother lines
        steps = max(1, int(dist / max(1, radius / 3)))

        for i in range(steps + 1):
            t = i / steps if steps > 0 else 0
            x = x1 + t * (x2 - x1)
            y = y1 + t * (y2 - y1)
            draw_soft_circle(x, y)

    # Draw soft circles at each point and lines between them
    for i in range(len(points)):
        draw_soft_circle(points[i][0], points[i][1])
        if i > 0:
            draw_line(points[i-1][0], points[i-1][1], points[i][0], points[i][1])

    layer.setPixelData(bytes(pixels), min_x, min_y, w, h)
    doc.refreshProjection()
    extra = _record(handle, "stroke", layer, (min_x, min_y, w, h), old)

    return {"color": "#%02x%02x%02x" % (r, g, b), "points_count": len(points), "hardness": hardness,
            "doc": handle, "layer": nodes.node_id(layer), **extra}


@command("fill", mutating=True)
def cmd_fill(params):
    """Fill a circular area with current color."""
    x = int_param(params, "x", 0, -COORD, COORD)
    y = int_param(params, "y", 0, -COORD, COORD)
    radius = int_param(params, "radius", 50, 1, COORD)  # the box is clipped to the document

    handle, doc, layer = _paint_target(params)
    r, g, b = _paint_rgb(params)

    # Paint a filled circle using pixel data
    # Create a bounding box
    x1 = max(0, x - radius)
    y1 = max(0, y - radius)
    x2 = min(doc.width(), x + radius)
    y2 = min(doc.height(), y + radius)
    w = x2 - x1
    h = y2 - y1

    if w <= 0 or h <= 0:
        raise CommandError("bad_request", "Fill area out of bounds")

    # Get existing pixel data (also the undo snapshot)
    old = pixelbuf.as_bytes(layer.pixelData(x1, y1, w, h))
    pixels = bytearray(old)

    # Draw circle
    for py in range(h):
        for px in range(w):
            # Check if point is in circle
            dx = (x1 + px) - x
            dy = (y1 + py) - y
            if dx*dx + dy*dy <= radius*radius:
                idx = (py * w + px) * 4
                pixels[idx] = b      # B
                pixels[idx+1] = g    # G
                pixels[idx+2] = r    # R
                pixels[idx+3] = 255  # A

    layer.setPixelData(bytes(pixels), x1, y1, w, h)
    doc.refreshProjection()
    extra = _record(handle, "fill", layer, (x1, y1, w, h), old)

    return {"color": "#%02x%02x%02x" % (r, g, b), "x": x, "y": y, "radius": radius, "doc": handle, "layer": nodes.node_id(layer),
            **extra}


@command("draw_shape", mutating=True)
def cmd_draw_shape(params):
    """Draw a shape (rectangle, ellipse, line)."""
    shape = params.get("shape", "rectangle")
    x = int_param(params, "x", 0, -COORD, COORD)
    y = int_param(params, "y", 0, -COORD, COORD)
    width = int_param(params, "width", 100, 0, COORD)
    height = int_param(params, "height", 100, 0, COORD)
    fill = params.get("fill", True)

    handle, doc, layer = _paint_target(params)
    r, g, b = _paint_rgb(params)
    written = None  # (rect, old bytes) of the write, for the journal

    if shape == "line":
        # Draw line using pixel data
        x2 = int_param(params, "x2", x + width, -COORD, COORD)
        y2 = int_param(params, "y2", y + height, -COORD, COORD)
        line_width = int_param(params, "line_width", 2, 1, MAX_BRUSH_SIZE)

        # Calculate bounding box
        x1_bound = max(0, int(min(x, x2)) - line_width)
        y1_bound = max(0, int(min(y, y2)) - line_width)
        x2_bound = min(doc.width(), int(max(x, x2)) + line_width)
        y2_bound = min(doc.height(), int(max(y, y2)) + line_width)
        w = x2_bound - x1_bound
        h = y2_bound - y1_bound

        if w > 0 and h > 0:
            old = pixelbuf.as_bytes(layer.pixelData(x1_bound, y1_bound, w, h))
            written = ((x1_bound, y1_bound, w, h), old)
            pixels = bytearray(old)

            # Draw line with thickness
            dist = max(abs(x2 - x), abs(y2 - y))
            steps = max(1, int(dist))
            radius = max(1, line_width // 2)

            for i in range(steps + 1):
                t = i / steps if steps > 0 else 0
                cx = x + t * (x2 - x)
                cy = y + t * (y2 - y)
                for dy in range(-radius, radius + 1):
                    for dx in range(-radius, radius + 1):
                        if dx*dx + dy*dy <= radius*radius:
                            px = int(cx) + dx - x1_bound
                            py = int(cy) + dy - y1_bound
                            if 0 <= px < w and 0 <= py < h:
                                idx = (py * w + px) * 4
                                pixels[idx] = b
                                pixels[idx+1] = g
                                pixels[idx+2] = r
                                pixels[idx+3] = 255

            layer.setPixelData(bytes(pixels), x1_bound, y1_bound, w, h)
    elif shape == "rectangle" and fill:
        # Draw filled rectangle using pixel data
        x1 = max(0, int(x))
        y1 = max(0, int(y))
        x2 = min(doc.width(), int(x + width))
        y2 = min(doc.height(), int(y + height))
        w = x2 - x1
        h = y2 - y1

        if w > 0 and h > 0:
            written = ((x1, y1, w, h), pixelbuf.as_bytes(layer.pixelData(x1, y1, w, h)))
            pixel_data = bytes((b, g, r, 255)) * (w * h)
            layer.setPixelData(pixel_data, x1, y1, w, h)
    elif shape == "ellipse" and fill:
        # Draw filled ellipse using pixel data
        cx = x + width / 2
        cy = y + height / 2
        rx = width / 2
        ry = height / 2

        x1 = max(0, int(x))
        y1 = max(0, int(y))
        x2 = min(doc.width(), int(x + width))
        y2 = min(doc.height(), int(y + height))
        w = x2 - x1
        h = y2 - y1

        if w > 0 and h > 0:
            old = pixelbuf.as_bytes(layer.pixelData(x1, y1, w, h))
            written = ((x1, y1, w, h), old)
            pixels = bytearray(old)

            for py in range(h):
                for px in range(w):
                    # Check if point is in ellipse
                    dx = (x1 + px - cx) / rx if rx > 0 else 0
                    dy = (y1 + py - cy) / ry if ry > 0 else 0
                    if dx*dx + dy*dy <= 1:
                        idx = (py * w + px) * 4
                        pixels[idx] = b
                        pixels[idx+1] = g
                        pixels[idx+2] = r
                        pixels[idx+3] = 255

            layer.setPixelData(bytes(pixels), x1, y1, w, h)
    else:
        raise CommandError("bad_request", f"Shape '{shape}' with current options not supported")

    doc.refreshProjection()
    extra = _record(handle, "draw_shape", layer, *written) if written else {}

    return {"color": "#%02x%02x%02x" % (r, g, b), "shape": shape, "doc": handle, "layer": nodes.node_id(layer), **extra}


# clear writes a full-canvas buffer, so it gets the long timeout.
@command("clear", timeout=LONG_TIMEOUT, mutating=True)
def cmd_clear(params):
    """Fill the layer's canvas area with a color, or with "transparent"
    erase it everywhere (the canvas plus anything past its edges)."""
    raw = params.get("color", "#1a1a2e")
    transparent = isinstance(raw, str) and raw.lower() == "transparent"
    color = None if transparent else _color_param(params, default="#1a1a2e")

    handle, doc, layer = _paint_target(params)
    canvas = QRect(0, 0, doc.width(), doc.height())

    if transparent:
        # bounds() is not clipped to the canvas; united() with an empty
        # (0, 0, 0, 0) rect gives the canvas.
        rect = canvas.united(layer.bounds())
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        old = pixelbuf.as_bytes(layer.pixelData(x, y, w, h))
        layer.setPixelData(bytes(w * h * 4), x, y, w, h)
        result_color = "transparent"
    else:
        x, y, w, h = 0, 0, canvas.width(), canvas.height()
        old = pixelbuf.as_bytes(layer.pixelData(x, y, w, h))
        pixel = bytes((color.blue(), color.green(), color.red(), 255))
        layer.setPixelData(pixel * (w * h), 0, 0, w, h)
        result_color = color.name()

    doc.refreshProjection()
    extra = _record(handle, "clear", layer, (x, y, w, h), old)

    return {"color": result_color, "doc": handle, "layer": nodes.node_id(layer), **extra}


@command("get_color_at")
def cmd_get_color_at(params):
    """Sample one pixel (eyedropper): the composite, or one layer."""
    x = int_param(params, "x", None, -COORD, COORD)
    y = int_param(params, "y", None, -COORD, COORD)

    handle, doc = registry.resolve_doc(params)
    width, height = doc.width(), doc.height()
    if not (0 <= x < width and 0 <= y < height):
        raise CommandError(
            "bad_request",
            f"({x}, {y}) is outside the document ({width}x{height}); "
            f"use 0 <= x < {width} and 0 <= y < {height}",
            {"x": x, "y": y, "width": width, "height": height})

    ref = params.get("layer")
    layer_id = None
    if ref is None:
        pixel_data = doc.rootNode().projectionPixelData(x, y, 1, 1)
    else:
        node = nodes.resolve_node(doc, ref)
        layer_id = nodes.node_id(node)
        kind = node.type()
        if kind == "paintlayer":
            pixel_data = node.pixelData(x, y, 1, 1)
        elif kind == "grouplayer":
            pixel_data = node.projectionPixelData(x, y, 1, 1)
        else:
            raise CommandError(
                "conflict",
                f"Layer '{node.name()}' is a {kind}; only paint layers and groups can be sampled",
                {"doc": handle, "layer": layer_id, "type": kind})

    pixel_data = bytes(pixel_data)  # QByteArray indexing gives bytes, not ints
    if len(pixel_data) >= 4:
        b, g, r, a = pixel_data[0], pixel_data[1], pixel_data[2], pixel_data[3]
        hex_color = "#{:02x}{:02x}{:02x}".format(r, g, b)
        return {"color": hex_color, "r": r, "g": g, "b": b, "a": a,
                "doc": handle, "layer": layer_id}

    raise CommandError("krita_error", "Could not read pixel")


@command("list_brushes")
def cmd_list_brushes(params):
    """List available brush presets."""
    filter_str = str(params.get("filter", ""))
    limit = int_param(params, "limit", 50, 1, MAX_LIMIT)

    presets = Krita.instance().resources("preset")
    brush_list = []

    for name, preset in presets.items():
        if filter_str.lower() in name.lower():
            brush_list.append(name)
            if len(brush_list) >= limit:
                break

    return {"brushes": brush_list, "count": len(brush_list)}
