"""Path checks for file-touching commands (D7). Pure: no krita / PyQt5 imports.

Paths are absolute, ``~``-prefixed, or relative to the output dir. Symlinks
are resolved (``realpath``) before the containment check, so they cannot
be used to escape. ``roots`` and ``base_dir`` default to
``config.current().roots`` and ``.output_dir``, read at call time.
"""

import os

from . import config
from .errors import CommandError

MANIFEST_EXT = ".json"  # allowed only where a command asks (export/create_layers manifests)


def resolve_allowed_path(path, must_exist=False, *, roots=None, base_dir=None, allow_json=False):
    """Resolve a file path and ensure it lies inside an allowed root and has
    an allowed extension (case-insensitive). Returns the realpath.

    ``allow_json`` also allows ``.json`` (the layer manifest). With
    ``must_exist`` the path must be an existing regular file.

    Errors: ``bad_request`` (no path), ``forbidden_path`` (outside every
    root, or extension not allowed), ``not_found`` (``must_exist``).
    """
    real, _ = _resolve_inside(path, roots, base_dir)
    ext = os.path.splitext(real)[1].lower()
    if ext not in config.ALLOWED_IMAGE_EXTS and not (allow_json and ext == MANIFEST_EXT):
        raise CommandError("forbidden_path", f"File type not allowed: {path}")
    if must_exist and not os.path.isfile(real):
        raise CommandError("not_found", f"File not found: {path}")
    return real


def resolve_output_dir(path, *, overwrite=False, create=True, roots=None, base_dir=None):
    """Resolve a directory output (``export_layers``) inside an allowed root.
    Returns the realpath.

    No extension check. A root itself is treated like any other directory.
    An existing directory must be empty unless ``overwrite``. A missing one
    is created (with parents) when ``create``, and containment is checked
    again afterwards, in case a symlink swapped in meanwhile sent it outside.

    Errors: ``bad_request`` (no path), ``forbidden_path`` (outside every
    root), ``conflict`` (not a directory, or not empty), ``not_found``
    (missing and not ``create``), ``krita_error`` (could not create).
    """
    real, roots = _resolve_inside(path, roots, base_dir)
    if os.path.lexists(real) and not os.path.isdir(real):
        raise CommandError("conflict", f"Not a directory: {path}")
    if os.path.isdir(real):
        if not overwrite and os.listdir(real):
            raise CommandError("conflict", f"Output directory is not empty: {path} (pass overwrite=true to write into it)")
        return real
    if not create:
        raise CommandError("not_found", f"Directory not found: {path}")
    try:
        os.makedirs(real, exist_ok=True)
    except OSError as e:
        raise CommandError("krita_error", f"Cannot create directory {path}: {e.strerror or e}") from e
    again = os.path.realpath(real)
    if not _inside(again, roots):
        raise CommandError("forbidden_path", f"Path is outside the allowed directories: {path}")
    return again


def _defaults(roots, base_dir):
    if roots is None or base_dir is None:
        settings = config.current()
        roots = settings.roots if roots is None else roots
        base_dir = settings.output_dir if base_dir is None else base_dir
    return roots, base_dir


def _resolve_inside(path, roots, base_dir):
    """Return ``(realpath, roots)``: the resolved path and the roots used."""
    if not isinstance(path, str) or not path:
        raise CommandError("bad_request", "No path specified")
    roots, base_dir = _defaults(roots, base_dir)
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base_dir, expanded)
    real = os.path.realpath(expanded)
    if not _inside(real, roots):
        raise CommandError("forbidden_path", f"Path is outside the allowed directories: {path}")
    return real, roots


def _inside(real, roots):
    for root in roots:
        root_real = os.path.realpath(root)
        if os.path.commonpath([real, root_real]) == root_real:
            return True
    return False
