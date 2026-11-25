"""Path checks for file-touching commands. Pure: no krita / PyQt5 imports."""

import os

from . import config
from .errors import CommandError


def resolve_allowed_path(path, must_exist=False, *, roots=None, base_dir=None):
    """Resolve a path and ensure it lies inside one of the allowed roots.

    Relative paths are resolved against ``base_dir``. Symlinks are
    resolved before the check, so they cannot be used to escape.

    ``roots`` and ``base_dir`` default to ``config.ALLOWED_DIRS`` and
    ``config.CANVAS_OUTPUT_DIR``, read at call time (overridable for tests).
    """
    if roots is None:
        roots = config.ALLOWED_DIRS
    if base_dir is None:
        base_dir = config.CANVAS_OUTPUT_DIR
    if not isinstance(path, str) or not path:
        raise CommandError("No path specified")
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base_dir, expanded)
    real = os.path.realpath(expanded)
    for root in roots:
        root_real = os.path.realpath(root)
        if os.path.commonpath([real, root_real]) == root_real:
            break
    else:
        raise CommandError(f"Path is outside the allowed directories: {path}")
    if os.path.splitext(real)[1].lower() not in config.ALLOWED_IMAGE_EXTS:
        raise CommandError(f"File type not allowed: {path}")
    if must_exist and not os.path.isfile(real):
        raise CommandError(f"File not found: {path}")
    return real
