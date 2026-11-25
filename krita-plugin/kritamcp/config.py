"""Plugin configuration (D7, D8). Pure: no krita / PyQt5 imports.

The module constants below are the fixed settings. The user-editable part
(allowed roots, limit overrides, journal size) comes from ``load()``, which
reads ``~/.krita-mcp-config.json`` and ``KRITA_MCP_ALLOWED_DIRS``, and from
``current()``, which caches that and re-loads when the inputs change.

Bad user config never raises: every problem goes into ``Settings.errors``,
which ``/health`` reports.
"""

import dataclasses
import json
import os
import stat
import threading
import types

SERVER_PORT = 5678
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")

# Directories that save/open_file may touch. Paths outside these are refused.
# (Superseded by current().roots; kept until every caller has moved.)
ALLOWED_DIRS = [CANVAS_OUTPUT_DIR]

# Shared secret; the MCP server reads the same file. Created on first run
# (by the extension at startup, not at import).
TOKEN_FILE = os.path.expanduser("~/.krita-mcp-token")
TOKEN_HEADER = "X-Krita-MCP-Token"

# Resource limits
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_CANVAS_DIM = 8192
MAX_CANVAS_PIXELS = 4096 * 4096
MAX_BRUSH_SIZE = 1000
MAX_STROKE_POINTS = 10000
MAX_LIMIT = 1000

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".kra", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".ora", ".psd"}
ALLOWED_HOSTS = {f"localhost:{SERVER_PORT}", f"127.0.0.1:{SERVER_PORT}"}

COORD = 10 * MAX_CANVAS_DIM  # generous bound for coordinates

# --- User config (D7) ---------------------------------------------------

CONFIG_FILE = os.path.expanduser("~/.krita-mcp-config.json")
CONFIG_FILE_ENV = "KRITA_MCP_CONFIG_FILE"  # overrides CONFIG_FILE (tests, scripted launches)
ALLOWED_DIRS_ENV = "KRITA_MCP_ALLOWED_DIRS"  # os.pathsep-separated

DEFAULT_LIMITS = {"max_canvas_dim": 16384, "max_canvas_pixels": 67108864}  # D8
DEFAULT_JOURNAL = {"max_actions": 20, "max_bytes": 1073741824}  # D6
KNOWN_KEYS = ("allowed_dirs", "limits", "journal")


@dataclasses.dataclass(frozen=True)
class Settings:
    """The resolved user config. ``limits`` and ``journal`` are read-only."""

    roots: tuple            # realpath'd allowed roots; roots[0] is output_dir
    output_dir: str         # realpath'd; may not exist yet
    limits: types.MappingProxyType
    journal: types.MappingProxyType
    errors: tuple           # human-readable problems, for /health
    config_file: str
    config_loaded: bool     # the file existed and was used
    requested_roots: tuple  # the extra roots as given (file, then env), valid or not


def load(config_file=None, env=None, output_dir=None, home=None):
    """Build ``Settings`` from the config file, the environment and the
    output dir. A pure function of its arguments plus the filesystem.

    Defaults (read at call time): ``config_file`` = ``$KRITA_MCP_CONFIG_FILE``
    or ``CONFIG_FILE``; ``env`` = ``os.environ``; ``output_dir`` =
    ``CANVAS_OUTPUT_DIR``; ``home`` = ``~``.
    """
    env = os.environ if env is None else env
    config_file = os.path.expanduser(config_file or env.get(CONFIG_FILE_ENV) or CONFIG_FILE)
    output_dir = os.path.realpath(os.path.expanduser(output_dir or CANVAS_OUTPUT_DIR))
    home = os.path.realpath(os.path.expanduser(home or "~"))
    errors = []
    limits = dict(DEFAULT_LIMITS)
    journal = dict(DEFAULT_JOURNAL)

    data, loaded = _read_config_file(config_file, errors)
    for key in data:
        if key not in KNOWN_KEYS:
            errors.append(f"config file {config_file}: unknown key {key!r} ignored")
    file_dirs = data.get("allowed_dirs", [])
    if not isinstance(file_dirs, list):
        errors.append(f"config file {config_file}: 'allowed_dirs' must be a list of strings; ignored")
        file_dirs = []
    _merge_ints(data, "limits", limits, config_file, errors)
    _merge_ints(data, "journal", journal, config_file, errors)

    requested = []
    for d in file_dirs:
        if isinstance(d, str):
            requested.append(d)
        else:
            errors.append(f"config file {config_file}: allowed_dirs entry {d!r} is not a string; ignored")
    requested += [d for d in env.get(ALLOWED_DIRS_ENV, "").split(os.pathsep) if d]

    roots = [output_dir]
    for raw in requested:
        expanded = os.path.expanduser(raw)
        if os.path.isabs(expanded) and os.path.realpath(expanded) in roots:
            continue  # duplicate (e.g. the output dir, which need not exist yet)
        real, why = check_root(raw, home)
        if why:
            errors.append(f"allowed root {raw!r} skipped: {why}")
        else:
            roots.append(real)

    return Settings(
        roots=tuple(roots), output_dir=output_dir,
        limits=types.MappingProxyType(limits), journal=types.MappingProxyType(journal),
        errors=tuple(errors), config_file=config_file, config_loaded=loaded,
        requested_roots=tuple(requested),
    )


def check_root(raw, home):
    """Return ``(realpath, None)`` for a usable root, else ``(None, reason)``.

    ``home`` must already be realpath'd. ``/``, the home directory and its
    ancestors are refused as too broad.
    """
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return None, "not an absolute path"
    real = os.path.realpath(expanded)
    if not os.path.exists(real):
        return None, "does not exist"
    if not os.path.isdir(real):
        return None, "not a directory"
    if os.path.dirname(real) == real:
        return None, "too broad (filesystem root)"
    if real == home:
        return None, "too broad (home directory)"
    if os.path.commonpath([real, home]) == real:
        return None, "too broad (contains the home directory)"
    return real, None


def _read_config_file(path, errors):
    """Return ``(data, loaded)``. A missing file is fine. A file that is not
    owned by us, is group/world-writable, or is not a JSON object is
    ignored as a whole, with an error.

    A symlinked config file is allowed if the link itself and its target
    are both owned by the user; the mode check applies to the target (a
    link's own mode bits mean nothing).
    """
    try:
        link = os.lstat(path)
    except FileNotFoundError:
        return {}, False
    except OSError as e:
        errors.append(f"config file {path} ignored: cannot stat ({e.strerror})")
        return {}, False
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):  # never open a FIFO or device
            errors.append(f"config file {path} ignored: not a regular file")
            return {}, False
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())  # the file actually read, not a re-stat of the name
            problem = _ownership_problem(link, st)
            if problem:
                errors.append(f"config file {path} ignored: {problem}")
                return {}, False
            raw = f.read()
    except FileNotFoundError:  # dangling symlink, or removed in between
        errors.append(f"config file {path} ignored: dangling symlink or vanished")
        return {}, False
    except OSError as e:
        errors.append(f"config file {path} ignored: cannot read ({e.strerror})")
        return {}, False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        errors.append(f"config file {path} ignored: invalid JSON ({e})")
        return {}, False
    if not isinstance(data, dict):
        errors.append(f"config file {path} ignored: top level must be a JSON object")
        return {}, False
    return data, True


def _ownership_problem(link, st):
    uid = os.getuid()
    if link.st_uid != uid:
        return "symlink is not owned by the current user"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_uid != uid:
        return "not owned by the current user"
    if st.st_mode & 0o022:
        return f"group- or world-writable (mode {stat.S_IMODE(st.st_mode):#o}); run chmod 600"
    return None


def _merge_ints(data, section, into, config_file, errors):
    """Overlay ``data[section]`` onto ``into``: known keys, positive ints."""
    if section not in data:
        return
    given = data[section]
    if not isinstance(given, dict):
        errors.append(f"config file {config_file}: {section!r} must be an object; ignored")
        return
    for key, value in given.items():
        if key not in into:
            errors.append(f"config file {config_file}: unknown key {section}.{key} ignored")
        elif type(value) is not int or value <= 0:
            errors.append(f"config file {config_file}: {section}.{key} must be a positive integer, "
                          f"got {value!r}; default {into[key]} kept")
        else:
            into[key] = value


# --- Cached access ------------------------------------------------------

_lock = threading.Lock()
_cache = None  # (key, settings, roots_state)


def current():
    """The current ``Settings``, re-loaded when an input changes: the config
    file (its identity, mtime, size, mode or owner, or the link's), the
    environment variables, ``CANVAS_OUTPUT_DIR``, or whether a requested
    root now resolves to a different place or (stops) existing. Safe to
    call from any thread.
    """
    global _cache
    with _lock:
        key = _inputs_key()
        if _cache is not None and _cache[0] == key and _cache[2] == _roots_state(_cache[1]):
            return _cache[1]
        settings = load()
        _cache = (key, settings, _roots_state(settings))  # pre-load key: a change mid-load reloads next time
        return settings


def reset_cache():
    global _cache
    with _lock:
        _cache = None


def _inputs_key():
    path = os.path.expanduser(os.environ.get(CONFIG_FILE_ENV) or CONFIG_FILE)
    return (path, CANVAS_OUTPUT_DIR, os.environ.get(ALLOWED_DIRS_ENV),
            _stat_key(path, os.lstat), _stat_key(path, os.stat))


def _stat_key(path, fn):
    try:
        st = fn(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _roots_state(settings):
    out = []
    for raw in settings.requested_roots:
        real = os.path.realpath(os.path.expanduser(raw))
        out.append((real, os.path.isdir(real)))
    return out
