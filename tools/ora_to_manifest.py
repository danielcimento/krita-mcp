#!/usr/bin/env python3
"""Convert an OpenRaster (.ora) file to a manifest v1 plus layer PNGs (P2.6).

    python tools/ora_to_manifest.py <file.ora> <out_dir> [--overwrite]

Writes ``<out_dir>/manifest.json`` (the format ``krita_export_layers``
writes and ``krita_create_layers(manifest=…)`` reads; D13) and one PNG per
paint layer. Runs outside Krita: stdlib only (``zipfile``, ``xml.etree``).

Mapping, following Krita's own ORA importer
(``plugins/impex/ora/kis_open_raster_stack_load_visitor.cpp``):

- ``stack.xml`` lists children top to bottom; the manifest lists them in
  create order: depth-first, each parent before its children, siblings
  bottom to top. ``stack_index`` 0 draws first.
- ``visibility`` → ``visible``; ``opacity`` → ``opacity`` (clamped 0–1);
  ``x``/``y`` → ``x``/``y``; ``composite-op`` → ``blend_mode`` via
  ``BLEND_OPS`` or ``krita:<id>`` → ``<id>``. An unknown op is an error.
- ``isolation="auto"`` → ``pass_through: true``; ``"isolate"`` or no
  attribute → ``false``. Krita's importer does exactly this, and Krita
  saved the Mao ``.ora`` (all ``auto``) with every group pass-through.
- ``svg:src-atop`` is Normal with alpha locked; ``alpha-preserve="true"``
  locks alpha; ``edit-locked="true"`` → ``locked`` (Krita's ORA export
  writes these three).

The input is untrusted: only PNGs named by ``src`` are read out of the zip,
``src`` may not be absolute or contain ``..``, and nothing is written
outside ``out_dir``. All problems are collected and reported together
before anything is written.
"""

import argparse
import json
import math
import os
import posixpath
import re
import shutil
import struct
import sys
import zipfile
import xml.etree.ElementTree as ET

GENERATOR = "krita-mcp ora_to_manifest"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# ORA composite-op → Krita composite-op ID. Verified 2026-11-25 against
# Krita's ORA importer (the `svg:` → COMPOSITE_* chain) and the ID strings
# in libs/pigment/KoCompositeOpRegistry.h. Note `svg:soft-light` is
# Krita's `soft_light_svg` (not the Photoshop `soft_light`), and
# `svg:luminosity` is `luminize`. `svg:add`, `color-dodge` and `difference`
# are legacy spellings the importer also accepts.
BLEND_OPS = {
    "svg:src-over": "normal",
    "svg:multiply": "multiply",
    "svg:plus": "add",
    "svg:screen": "screen",
    "svg:overlay": "overlay",
    "svg:darken": "darken",
    "svg:lighten": "lighten",
    "svg:color-dodge": "dodge",
    "svg:color-burn": "burn",
    "svg:hard-light": "hard_light",
    "svg:soft-light": "soft_light_svg",
    "svg:difference": "diff",
    "svg:color": "color",
    "svg:luminosity": "luminize",
    "svg:hue": "hue",
    "svg:saturation": "saturation",
    "svg:dst-in": "destination-in",
    "svg:dst-out": "erase",
    "svg:dst-atop": "destination-atop",
    "svg:add": "add",
    "color-dodge": "dodge",
    "difference": "diff",
}
# Krita writes Normal + alpha lock as svg:src-atop.
SRC_ATOP = "svg:src-atop"
_KRITA_ID_RE = re.compile(r"[a-z0-9_-]+")


class OraError(Exception):
    """The .ora can't be converted. ``problems`` lists every reason."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def escape_name(name):
    """Escape one layer name as a path component (same as kritamcp.refs)."""
    return name.replace("\\", "\\\\").replace("/", "\\/")


def slug(name, max_len=60):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "layer"


def _safe_member(src):
    """Normalized zip member name for ``src``, or None if it is unsafe."""
    if not src or "\\" in src or "\x00" in src:
        return None
    if src.startswith("/") or re.match(r"[A-Za-z]:", src):
        return None
    if ".." in src.split("/"):
        return None
    norm = posixpath.normpath(src)
    if norm in (".", "") or norm.startswith("../") or norm.startswith("/"):
        return None
    return norm


def _png_size(head):
    """(w, h) from a PNG's first 24 bytes, or None if it isn't a PNG."""
    if len(head) < 24 or head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def _int_attr(elem, attr, where, problems, default=0):
    raw = elem.get(attr)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        f = float(raw)
        if f.is_integer():
            return int(f)
    except ValueError:
        pass
    problems.append(f"{where}: {attr}={raw!r} is not an integer")
    return default


def _common(elem, where, problems):
    vis = elem.get("visibility", "visible")
    if vis not in ("visible", "hidden"):
        problems.append(f"{where}: visibility={vis!r} (expected visible or hidden)")
    raw = elem.get("opacity")
    opacity = 1.0
    if raw is not None:
        try:
            opacity = float(raw)
        except ValueError:
            opacity = math.nan
        if math.isnan(opacity):
            problems.append(f"{where}: opacity={raw!r} is not a number")
            opacity = 1.0
        opacity = min(1.0, max(0.0, opacity))
    op = elem.get("composite-op", "svg:src-over")
    alpha_locked = elem.get("alpha-preserve") == "true"
    if op == SRC_ATOP:
        blend, alpha_locked = "normal", True
    elif op in BLEND_OPS:
        blend = BLEND_OPS[op]
    elif op.startswith("krita:") and _KRITA_ID_RE.fullmatch(op[6:]):
        blend = op[6:]
    else:
        problems.append(f"{where}: unknown composite-op {op!r}")
        blend = None
    return {
        "visible": vis != "hidden",
        "opacity": opacity,
        "blend_mode": blend,
        "alpha_locked": alpha_locked,
        "locked": elem.get("edit-locked") == "true",
        "op": op,
    }


def _parse(zf, ora_name):
    """Parse stack.xml into (document, layers, members); raises OraError."""
    try:
        raw = zf.read("stack.xml")
    except KeyError:
        raise OraError([f"{ora_name}: no stack.xml"]) from None
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise OraError(["stack.xml: DOCTYPE/ENTITY declarations are not allowed"])
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise OraError([f"stack.xml: {e}"]) from None
    if root.tag != "image":
        raise OraError([f"stack.xml: root element is <{root.tag}>, expected <image>"])

    problems = []
    width = _int_attr(root, "w", "<image>", problems, default=None)
    height = _int_attr(root, "h", "<image>", problems, default=None)
    if not width or not height or width <= 0 or height <= 0:
        problems.append("<image>: w and h must be positive integers")
    stacks = [c for c in root if c.tag == "stack"]
    if len(stacks) != 1 or len(root) != 1:
        problems.append("<image>: expected exactly one root <stack>")
        raise OraError(problems)

    document = {"name": os.path.splitext(os.path.basename(ora_name))[0],
                "width": width, "height": height}
    xres, yres = root.get("xres"), root.get("yres")
    if xres is not None and xres == yres:
        try:
            r = float(xres)
            document["resolution"] = int(r) if r.is_integer() else r
        except ValueError:
            problems.append(f"<image>: xres={xres!r} is not a number")

    names_in_zip = set(zf.namelist())
    layers, members = [], {}

    def walk(stack, parent_key, parent_names):
        for elem in reversed(list(stack)):
            idx = len(layers) + 1
            key = f"L{idx:04d}"
            name = elem.get("name")
            label = "/".join(escape_name(n) for n in parent_names + [name or "?"])
            where = f"<{elem.tag}> {label!r}"
            if elem.tag not in ("stack", "layer"):
                problems.append(f"{where}: unsupported element <{elem.tag}>")
                continue
            if name is None:
                problems.append(f"{where}: missing name")
                name = ""
            names = parent_names + [name]
            c = _common(elem, where, problems)
            entry = {
                "key": key,
                "name": name,
                "path": "/".join(escape_name(n) for n in names),
                "type": "group" if elem.tag == "stack" else "paint",
                "parent": parent_key,
                "visible": c["visible"],
                "opacity": c["opacity"],
                "blend_mode": c["blend_mode"],
            }
            layers.append(entry)
            if elem.tag == "stack":
                iso = elem.get("isolation")
                if iso not in (None, "auto", "isolate"):
                    problems.append(f"{where}: isolation={iso!r} (expected auto or isolate)")
                if c["alpha_locked"]:
                    problems.append(f"{where}: alpha lock ({c['op']} / alpha-preserve) "
                                    "is not supported on groups")
                entry["pass_through"] = iso == "auto"
                if c["locked"]:
                    entry["locked"] = True
                walk(elem, "$" + key, names)
            else:
                if elem.get("type") is not None:
                    problems.append(f"{where}: unsupported layer type {elem.get('type')!r}")
                entry.update(inherit_alpha=False, alpha_locked=c["alpha_locked"],
                             locked=c["locked"])
                src = elem.get("src")
                png = w = h = None
                if src is not None:
                    member = _safe_member(src)
                    if member is None:
                        problems.append(f"{where}: unsafe src {src!r}")
                    elif member not in names_in_zip:
                        problems.append(f"{where}: src {src!r} is not in the archive")
                    else:
                        with zf.open(member) as f:
                            size = _png_size(f.read(24))
                        if size is None:
                            problems.append(f"{where}: src {src!r} is not a PNG")
                        else:
                            w, h = size
                            png = f"{idx:04d}_{slug(name)}.png"
                            members[png] = member
                entry.update(png=png,
                             x=_int_attr(elem, "x", where, problems),
                             y=_int_attr(elem, "y", where, problems),
                             w=w, h=h)

    walk(stacks[0], "/", [])
    if problems:
        raise OraError(problems)
    for i, entry in enumerate(layers):
        entry["stack_index"] = i
    return document, layers, members


def summarize(manifest):
    layers = manifest["layers"]
    groups = [l for l in layers if l["type"] == "group"]
    depth = {}  # key -> group nesting depth; a top-level group is 1
    for g in groups:
        depth[g["key"]] = 1 if g["parent"] == "/" else depth[g["parent"][1:]] + 1
    names = {}
    for l in layers:
        names[l["name"]] = names.get(l["name"], 0) + 1
    return {
        "paint": sum(1 for l in layers if l["type"] == "paint"),
        "groups": len(groups),
        "max_group_depth": max(depth.values(), default=0),
        "hidden": [l["path"] for l in layers if not l["visible"]],
        "blend_modes": {m: sum(1 for l in layers if l["blend_mode"] == m)
                        for m in sorted({l["blend_mode"] for l in layers})},
        "duplicate_names": sorted(n for n, k in names.items() if k > 1),
    }


def convert(ora_path, out_dir, overwrite=False):
    """Convert ``ora_path`` into ``out_dir``; return the manifest dict.

    ``out_dir`` must be missing or empty unless ``overwrite``. Raises
    ``OraError`` (listing every problem) before writing anything if the
    file can't be converted.
    """
    if os.path.exists(out_dir):
        if not os.path.isdir(out_dir):
            raise OraError([f"{out_dir}: exists and is not a directory"])
        if os.listdir(out_dir) and not overwrite:
            raise OraError([f"{out_dir}: not empty (pass overwrite to write into it)"])
    try:
        zf = zipfile.ZipFile(ora_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise OraError([f"{ora_path}: {e}"]) from None
    with zf:
        document, layers, members = _parse(zf, ora_path)
        os.makedirs(out_dir, exist_ok=True)
        real_out = os.path.realpath(out_dir)
        for png, member in members.items():
            dest = os.path.realpath(os.path.join(real_out, png))
            if os.path.dirname(dest) != real_out:
                raise OraError([f"{png}: would be written outside {out_dir}"])
            with zf.open(member) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
    manifest = {"version": 1, "generator": GENERATOR, "document": document,
                "layers": layers}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write("\n")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert an .ora to manifest v1 plus PNGs.")
    ap.add_argument("ora")
    ap.add_argument("out_dir")
    ap.add_argument("--overwrite", action="store_true",
                    help="write into a non-empty out_dir")
    args = ap.parse_args(argv)
    try:
        manifest = convert(args.ora, args.out_dir, overwrite=args.overwrite)
    except OraError as e:
        for p in e.problems:
            print(f"error: {p}", file=sys.stderr)
        return 1
    s = summarize(manifest)
    d = manifest["document"]
    print(f"{args.out_dir}/manifest.json: {s['paint']} paint, {s['groups']} groups "
          f"(max depth {s['max_group_depth']}), {d['width']}x{d['height']}, "
          f"blend {s['blend_modes']}, {len(s['hidden'])} hidden, "
          f"{len(s['duplicate_names'])} duplicate names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
