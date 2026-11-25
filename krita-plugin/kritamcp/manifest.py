"""Layer manifest v1 (D13): read, check structure, turn into create_layers specs.

Pure: stdlib only, no krita / PyQt5 imports.

``export_layers`` writes a manifest (``build`` + ``write``) and
``create_layers(manifest=…)`` reads one (``read`` + ``to_specs``). The
schema is in api-design.md § ``krita_export_layers``. Only structure is
checked here; field values (opacity ranges, blend IDs, …) are checked by
``create_layers`` like any other spec, and every ``png`` still goes
through ``paths.resolve_allowed_path``.

Rules (api-design.md § Manifest schema, P2.4 note):

- ``version`` must be 1. Top-level keys other than ``version``,
  ``generator``, ``document`` and ``layers`` are rejected.
- Every entry has a unique ``key``. ``parent`` is ``"/"`` (the default) or
  ``"$<key>"`` of an earlier group entry; ``above`` is ``"$<key>"`` of an
  earlier sibling. Names and UUIDs are not accepted: IDs don't carry over
  between documents and names can be ambiguous.
- Entry keys outside ``SPEC_FIELDS`` and ``INFO_FIELDS`` are rejected.
  ``INFO_FIELDS`` are accepted and dropped.
- Entries with ``"projection": true`` (group projections from
  ``export_layers(include_groups=True)``) are skipped, and may not be a
  ``parent`` or ``above`` target.
- ``png`` is relative to the manifest's directory, or ``null`` for an
  empty layer.
"""

import json
import os
import tempfile
from typing import NamedTuple

from . import batch
from .errors import CommandError

VERSION = 1
GENERATOR = "krita-mcp"
MAX_BYTES = 16 * 1024 * 1024

# The create_layers spec fields (api-design.md § krita_create_layers).
SPEC_FIELDS = frozenset({
    "key", "name", "type", "parent", "above", "visible", "opacity", "blend_mode",
    "locked", "alpha_locked", "inherit_alpha", "pass_through", "png", "x", "y",
})
# Informative manifest-only entry fields: accepted, then dropped.
INFO_FIELDS = frozenset({"id", "path", "stack_index", "w", "h", "projection"})
TOP_FIELDS = frozenset({"version", "generator", "document", "layers"})
TYPES = ("paint", "group")

_ROOT = "/"


class Specs(NamedTuple):
    """``to_specs`` result: ``specs, skipped, document = to_specs(...)``."""

    specs: list      # create_layers specs, in manifest order
    skipped: list    # [{"index", "key", "reason"}]
    document: dict   # the manifest's ``document`` ({} if absent); informative


def _bad(i, msg, **details):
    return CommandError("bad_request", f"layers[{i}]: {msg}", {"index": i, **details})


def _no_dup_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def read(path):
    """Load a manifest from an already-resolved ``path``; return the dict.

    Errors: ``too_large`` over ``MAX_BYTES``; ``bad_request`` for bad
    UTF-8, invalid JSON (``details`` has ``line``, ``column``, ``pos``),
    duplicate object keys, NaN/Infinity, or a top level that isn't an
    object; ``not_found`` / ``krita_error`` if the file can't be read.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        raise CommandError("not_found", f"Manifest not found: {path}") from None
    except OSError as e:
        raise CommandError("krita_error", f"Could not read manifest {path}: {e}") from None
    if len(raw) > MAX_BYTES:
        raise CommandError("too_large", f"Manifest {path} is over {MAX_BYTES} bytes",
                           {"limit": MAX_BYTES})
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CommandError("bad_request", f"Manifest {path} is not UTF-8 (byte {e.start})",
                           {"pos": e.start}) from None
    try:
        data = json.loads(text, object_pairs_hook=_no_dup_pairs, parse_constant=_no_constant)
    except json.JSONDecodeError as e:
        raise CommandError("bad_request",
                           f"Manifest {path} is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}",
                           {"line": e.lineno, "column": e.colno, "pos": e.pos}) from None
    except ValueError as e:
        raise CommandError("bad_request", f"Manifest {path}: {e}") from None
    if not isinstance(data, dict):
        raise CommandError("bad_request", f"Manifest {path}: the top level must be a JSON object")
    return data


def _key_ref(i, entry, field, earlier, all_keys):
    """Key named by ``entry[field]`` (a ``"$key"`` of an earlier entry)."""
    ref = entry[field]
    if not isinstance(ref, str) or not ref.startswith("$") or len(ref) < 2:
        raise _bad(i, f"{field} {ref!r} must be '$<key>' of an earlier entry"
                      + (" or '/'" if field == "parent" else "")
                      + "; a manifest links layers by key only, not by name or id",
                   field=field)
    key = ref[1:]
    if key not in earlier:
        raise _bad(i, f"{field} {ref!r}: no earlier entry has key {key!r}"
                      + (" (it is defined later; parents and siblings must come first)"
                         if key in all_keys else ""),
                   field=field)
    return key


def to_specs(manifest, manifest_dir):
    """Check a manifest's structure and return ``Specs(specs, skipped, document)``.

    ``manifest_dir`` is the directory ``png`` paths are relative to. Specs
    hold only ``SPEC_FIELDS``, with ``png`` joined onto ``manifest_dir``
    and normalized (not realpath'd), or left out when ``null``.

    Errors are ``CommandError``: ``bad_request`` (with ``details.index``
    when an entry is at fault), ``too_large`` over ``batch.MAX_ITEMS``.
    """
    if not isinstance(manifest, dict):
        raise CommandError("bad_request", "manifest must be a JSON object")
    version = manifest.get("version")
    if version != VERSION or isinstance(version, bool):
        got = "missing" if "version" not in manifest else repr(version)
        raise CommandError("bad_request",
                           f"manifest version {got} is not supported; supported versions: {VERSION}",
                           {"version": version, "supported": [VERSION]})
    extra = sorted(set(manifest) - TOP_FIELDS)
    if extra:
        raise CommandError("bad_request", f"manifest: unknown top-level keys {extra}",
                           {"keys": extra})
    document = manifest.get("document", {})
    if not isinstance(document, dict):
        raise CommandError("bad_request", "manifest document must be an object")
    if "generator" in manifest and not isinstance(manifest["generator"], str):
        raise CommandError("bad_request", "manifest generator must be a string")
    layers = manifest.get("layers")
    batch.check_items(layers, field="manifest layers")

    base = os.path.abspath(manifest_dir)
    # Every key, so a forward reference can be told apart from a missing key.
    all_keys = {e.get("key") for e in layers if isinstance(e, dict)}
    earlier = {}   # key -> {"type", "parent", "projection"} of entries seen so far
    specs, skipped = [], []
    for i, entry in enumerate(layers):
        if not isinstance(entry, dict):
            raise _bad(i, "each entry must be an object")
        unknown = sorted(set(entry) - SPEC_FIELDS - INFO_FIELDS)
        if unknown:
            raise _bad(i, f"unknown keys {unknown}", keys=unknown)

        key = entry.get("key")
        if not isinstance(key, str) or key == "":
            raise _bad(i, "key is required in a manifest and must be a non-empty string",
                       field="key")
        if key in earlier:
            raise _bad(i, f"duplicate key {key!r}", field="key", key=key)

        typ = entry.get("type", "paint")
        if typ not in TYPES:
            raise _bad(i, f"type {typ!r} must be one of {list(TYPES)}", field="type")

        parent = _ROOT
        if "parent" in entry and entry["parent"] != _ROOT:
            pkey = _key_ref(i, entry, "parent", earlier, all_keys)
            p = earlier[pkey]
            if p["projection"]:
                raise _bad(i, f"parent '${pkey}' is a projection entry, which is skipped",
                           field="parent")
            if p["type"] != "group":
                raise _bad(i, f"parent '${pkey}' is not a group", field="parent")
            parent = "$" + pkey

        if "above" in entry:
            akey = _key_ref(i, entry, "above", earlier, all_keys)
            a = earlier[akey]
            if a["projection"]:
                raise _bad(i, f"above '${akey}' is a projection entry, which is skipped",
                           field="above")
            if a["parent"] != parent:
                raise _bad(i, f"above '${akey}' is not a sibling (its parent is "
                              f"{a['parent']!r}, this entry's is {parent!r})", field="above")

        projection = entry.get("projection", False)
        if not isinstance(projection, bool):
            raise _bad(i, "projection must be true or false", field="projection")

        png = entry.get("png")
        if png is not None:
            if not isinstance(png, str) or png == "" or "\x00" in png:
                raise _bad(i, "png must be a non-empty path string or null", field="png")
            if os.path.isabs(png):
                raise _bad(i, f"png {png!r}: png paths in a manifest are relative to the manifest",
                           field="png")

        earlier[key] = {"type": typ, "parent": parent, "projection": projection}
        if projection:
            skipped.append({"index": i, "key": key,
                            "reason": "group projection (export_layers include_groups)"})
            continue
        spec = {k: v for k, v in entry.items() if k in SPEC_FIELDS and k != "png"}
        if png is not None:
            spec["png"] = os.path.normpath(os.path.join(base, png))
        specs.append(spec)
    return Specs(specs, skipped, document)


def build(document, layers):
    """A manifest v1 dict for ``export_layers``."""
    return {"version": VERSION, "generator": GENERATOR, "document": document, "layers": layers}


def write(path, manifest):
    """Write ``manifest`` to ``path`` as UTF-8 JSON, atomically."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, ensure_ascii=False, allow_nan=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
