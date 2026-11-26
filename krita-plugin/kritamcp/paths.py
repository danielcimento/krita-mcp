"""Path checks for file-touching commands (D7). Pure: no krita / PyQt5 imports.

Paths are absolute, ``~``-prefixed, or relative to the output dir. Symlinks
are resolved (``realpath``) before the containment check, so they cannot
be used to escape. ``roots`` and ``base_dir`` default to
``config.current().roots`` and ``.output_dir``, read at call time.
"""

import json
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
        allowed = sorted(config.ALLOWED_IMAGE_EXTS | ({MANIFEST_EXT} if allow_json else set()))
        raise CommandError("forbidden_path",
                           f"File type not allowed: {path} (extension {ext or '(none)'!r}); "
                           f"allowed here: {', '.join(allowed)}",
                           {"path": path, "extension": ext, "allowed_extensions": allowed})
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
    real, roots = _resolve_inside(path, roots, base_dir, directory=True)
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
        raise forbidden(path, again, roots, directory=True)
    return again


def ensure_parent_dir(real, *, roots=None):
    """Create the missing parent directories of ``real`` (a file path that
    ``resolve_allowed_path`` returned), then check containment again, in
    case a symlink swapped in meanwhile sent it outside. Returns the
    realpath again.

    Errors: ``conflict`` (a parent is a file), ``krita_error`` (could not
    create), ``forbidden_path`` (outside every root after creating).
    """
    if roots is None:
        roots = config.current().roots
    parent = os.path.dirname(real)
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except FileExistsError:
            raise CommandError("conflict", f"Not a directory: {parent}") from None
        except NotADirectoryError:
            raise CommandError("conflict", f"A parent of {parent} is not a directory") from None
        except OSError as e:
            raise CommandError("krita_error",
                               f"Cannot create directory {parent}: {e.strerror or e}") from e
    again = os.path.realpath(real)
    if not _inside(again, roots):
        raise forbidden(real, again, roots)
    return again


def _defaults(roots, base_dir):
    if roots is None or base_dir is None:
        settings = config.current()
        roots = settings.roots if roots is None else roots
        base_dir = settings.output_dir if base_dir is None else base_dir
    return roots, base_dir


def _resolve_inside(path, roots, base_dir, directory=False):
    """Return ``(realpath, roots)``: the resolved path and the roots used."""
    if not isinstance(path, str) or not path:
        raise CommandError("bad_request", "No path specified")
    roots, base_dir = _defaults(roots, base_dir)
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base_dir, expanded)
    real = os.path.realpath(expanded)
    if not _inside(real, roots):
        raise forbidden(path, real, roots, directory=directory)
    return real, roots


def _inside(real, roots):
    for root in roots:
        root_real = os.path.realpath(root)
        if os.path.commonpath([real, root_real]) == root_real:
            return True
    return False


# --- forbidden_path errors (F003 K2) -----------------------------------------

def _suggested_dir(real, directory):
    """The folder to add to ``allowed_dirs`` so ``real`` is allowed: the
    path itself for a directory output, else its parent; walked up to the
    nearest folder that exists (a root must exist), and None when that is
    the home directory, one of its ancestors or ``/`` (config refuses those
    as too broad). Returns ``(folder, exists)``: ``exists`` False when the
    wanted folder (path or parent) has to be created first."""
    wanted = real if directory else os.path.dirname(real)
    folder = wanted
    while not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return None, False
        folder = parent
    home = os.path.realpath(os.path.expanduser("~"))
    if os.path.dirname(folder) == folder or os.path.commonpath([folder, home]) == folder:
        return None, folder == wanted
    return folder, folder == wanted


def _display(path):
    """``path`` with the home directory written as ``~``."""
    home = os.path.realpath(os.path.expanduser("~"))
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def forbidden(given, real, roots, directory=False):
    """The ``forbidden_path`` error for a path outside every allowed root:
    the message lists the allowed folders, the config file and its key,
    and a one-line fix with the JSON to write (an agent can act on it
    without reading docs). ``details`` repeats it as data."""
    settings = config.current()
    allowed = [os.path.realpath(r) for r in roots]
    config_file = settings.config_file
    exists = os.path.isfile(config_file)
    folder, folder_exists = _suggested_dir(real, directory)
    key = config.ALLOWED_DIRS_KEY
    lines = [f"{given} is outside the allowed directories",
             f"  allowed: {', '.join(allowed)}"]
    if real != os.path.abspath(os.path.expanduser(given)):
        lines[0] += f" (it resolves to {real})"
    if folder is None:
        lines.append(f"  to allow it: add a folder that contains it (not your home folder or "
                     f"above) to \"{key}\" in {config_file}; it applies from the next call, "
                     "no restart needed")
    else:
        entries = list(settings.file_dirs)
        if _display(folder) not in entries and folder not in entries:
            entries.append(_display(folder))
        example = json.dumps({key: entries})
        verb = "add the folder" if exists else "create the file with"
        lines.append(f"  to allow it: {verb} \"{folder}\" in \"{key}\" in {config_file}, "
                     f"e.g. {example}; it applies from the next call, no restart needed "
                     "(krita_reload_config shows the result)")
        if not folder_exists:
            lines.append(f"  note: {real if directory else os.path.dirname(real)} does not exist "
                         f"yet; {folder} is the nearest folder that does (a folder in "
                         f"\"{key}\" must exist)")
    skipped = [e for e in settings.errors if e.startswith("allowed root ")]
    if skipped:
        lines.append("  config problems: " + "; ".join(skipped))
    details = {"path": given, "resolved": real, "allowed_dirs": allowed,
               "config_file": config_file, "config_key": key, "config_exists": exists,
               "suggested_dir": folder}
    return CommandError("forbidden_path", "\n".join(lines), details)
