"""Plugin configuration (D7, D8). Pure: no krita / PyQt5 imports.

The module constants below are the fixed settings. The user-editable part
(allowed roots, limit overrides, journal size, lint settings, the Live2D
conventions) comes from ``load()``, which
reads ``~/.krita-mcp-config.json`` and ``KRITA_MCP_ALLOWED_DIRS``, and from
``current()``, which caches that and re-loads when the inputs change.
Commands read ``current()`` at call time: ``roots`` and ``output_dir`` for
paths, ``limits`` for the canvas size, ``lint`` and ``live2d`` for the lint
snapshot, ``live2d.enabled`` for the convention-specific warnings (F004).

Bad user config never raises: every problem goes into ``Settings.errors``,
which ``/health`` reports.
"""

import dataclasses
import json
import os
import re
import stat
import threading
import types

SERVER_PORT = 5678
# The default output dir and always the first allowed root. Commands use
# current().output_dir (realpath'd), not this.
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")

# Shared secret; the MCP server reads the same file. Created on first run
# (by the extension at startup, not at import).
TOKEN_FILE = os.path.expanduser("~/.krita-mcp-token")
TOKEN_HEADER = "X-Krita-MCP-Token"

# Resource limits. The canvas size limits are configurable: see
# DEFAULT_LIMITS below and current().limits.
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_BRUSH_SIZE = 1000
MAX_STROKE_POINTS = 10000
MAX_LIMIT = 1000

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".kra", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".ora", ".psd"}
ALLOWED_HOSTS = {f"localhost:{SERVER_PORT}", f"127.0.0.1:{SERVER_PORT}"}

# --- User config (D7) ---------------------------------------------------

CONFIG_FILE = os.path.expanduser("~/.krita-mcp-config.json")
CONFIG_FILE_ENV = "KRITA_MCP_CONFIG_FILE"  # overrides CONFIG_FILE (tests, scripted launches)
ALLOWED_DIRS_ENV = "KRITA_MCP_ALLOWED_DIRS"  # os.pathsep-separated

DEFAULT_LIMITS = {"max_canvas_dim": 16384, "max_canvas_pixels": 67108864}  # D8
# D6. max_bytes: pixel snapshots kept in memory; max_disk_bytes: the total
# spilled to PNGs under the state directory beyond that.
DEFAULT_JOURNAL = {"max_actions": 20, "max_bytes": 1073741824, "max_disk_bytes": 4294967296}
# Lint settings (D14; F004 G3), copied into the lint snapshot's ``settings``.
# Generic (category 2, F004 survey.md):
# stray_alpha_*: a pixel with 0 < alpha < below farther than distance px
# from any pixel with alpha >= below is stray (api-design § Lint rules).
# stray_speck_max_px: an 8-connected group of stray pixels this size or
# smaller is a speck (a finding); larger ones are soft glows, not flagged
# (P5.10). 64 px (8x8) covers stray dabs and eraser leftovers; a glow that
# fades out beyond the distance is a ring of hundreds of pixels or more.
# reference_pattern: a group whose trimmed name matches it (re.search) is
# reference material, skipped with its contents by every rule; null: none.
# disable: rule ids left out of the default/conventions/all sets (opt-out).
DEFAULT_LINT = {"stray_alpha_below": 8, "stray_alpha_distance": 4, "stray_speck_max_px": 64,
                "reference_pattern": r"^\[.*\]$", "disable": ()}
LINT_INT_RANGES = {"stray_alpha_below": (1, 255), "stray_alpha_distance": (1, 64),
                   "stray_speck_max_px": (1, 1_000_000)}

# The Live2D rig conventions (category 3, opt-in; live2d-workspace
# CONVENTIONS.md). enabled: the lint pack "live2d" is in the rule sets,
# makes duplicate-name an error with pose-suffix suggestions, and
# set_canvas_size warns that it breaks rigs. The other keys are that
# pack's knobs (lint/live2d.py has their meaning).
DEFAULT_LIVE2D = {"enabled": False, "additive_mode": "either",
                  "allowed_blend_modes": ("normal", "multiply", "add", "linear_dodge"),
                  "max_nesting": 3, "pose_pattern": r"^(?P<stem>.*\S) (?P<pose>[A-KM-QS-Z])$",
                  "default_pose": "A"}
LIVE2D_ADDITIVE_MODES = ("either", "add", "linear_dodge")
LIVE2D_MAX_NESTING = 64

# Static sanity bound for coordinates and radii in the painting tools, ten
# times the default per-side limit. Not affected by the config file.
COORD = 10 * DEFAULT_LIMITS["max_canvas_dim"]
KNOWN_KEYS = ("allowed_dirs", "limits", "journal", "lint", "live2d")
# The config-file key that lists extra allowed directories (K2: named in
# every forbidden_path error and in /health).
ALLOWED_DIRS_KEY = "allowed_dirs"


@dataclasses.dataclass(frozen=True)
class Settings:
    """The resolved user config. ``limits``, ``journal``, ``lint`` and
    ``live2d`` are read-only (lists in them are tuples)."""

    roots: tuple            # realpath'd allowed roots; roots[0] is output_dir
    output_dir: str         # realpath'd; may not exist yet
    limits: types.MappingProxyType
    journal: types.MappingProxyType
    errors: tuple           # human-readable problems, for /health
    config_file: str
    config_loaded: bool     # the file existed and was used
    requested_roots: tuple  # the extra roots as given (file, then env), valid or not
    lint: types.MappingProxyType = types.MappingProxyType(DEFAULT_LINT)
    file_dirs: tuple = ()   # the config file's allowed_dirs strings, as written
    live2d: types.MappingProxyType = types.MappingProxyType(DEFAULT_LIVE2D)


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
    lint = dict(DEFAULT_LINT)
    live2d = dict(DEFAULT_LIVE2D)

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
    legacy = _merge_lint(data, lint, config_file, errors)
    _merge_live2d(data, live2d, legacy, config_file, errors)

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
        requested_roots=tuple(requested), lint=types.MappingProxyType(lint),
        file_dirs=tuple(d for d in file_dirs if isinstance(d, str)),
        live2d=types.MappingProxyType(live2d),
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


def _merge_lint(data, into, config_file, errors):
    """Overlay ``data["lint"]`` onto ``into``: the thresholds ints within
    ``LINT_INT_RANGES``, ``reference_pattern`` a regular expression or
    null, ``disable`` a list of known rule ids. A bad value keeps its
    default, with an error. Returns the legacy ``lint.additive_mode`` (F004:
    now ``live2d.additive_mode``), or None."""
    if "lint" not in data:
        return None
    given = data["lint"]
    if not isinstance(given, dict):
        errors.append(f"config file {config_file}: 'lint' must be an object; ignored")
        return None
    legacy = None
    for key, value in given.items():
        if key == "additive_mode":
            legacy = value  # checked by _merge_live2d
        elif key not in into:
            errors.append(f"config file {config_file}: unknown key lint.{key} ignored")
        elif key == "reference_pattern":
            problem = _regex_problem(value, allow_none=True)
            if problem:
                errors.append(f"config file {config_file}: lint.reference_pattern {problem}, "
                              f"got {value!r}; default {into[key]!r} kept")
            else:
                into[key] = value
        elif key == "disable":
            into[key] = _rule_ids(value, config_file, errors)
        else:
            lo, hi = LINT_INT_RANGES[key]
            if type(value) is int and lo <= value <= hi:
                into[key] = value
            else:
                errors.append(f"config file {config_file}: lint.{key} must be an integer from {lo} to {hi}, "
                              f"got {value!r}; default {into[key]} kept")
    return legacy


def _regex_problem(value, allow_none=False, groups=()):
    """Why ``value`` isn't a usable regular expression, or None."""
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        return "must be a regular expression (a string)" + (" or null" if allow_none else "")
    try:
        compiled = re.compile(value)
    except re.error as e:
        return f"is not a valid regular expression ({e})"
    missing = [g for g in groups if g not in compiled.groupindex]
    if missing:
        return "needs the named groups " + ", ".join(f"(?P<{g}>...)" for g in missing)
    return None


def _rule_ids(value, config_file, errors):
    """``lint.disable`` as a tuple of known rule ids; others are dropped
    with an error."""
    from . import lint  # pure; imported here so config stays importable on its own

    if not isinstance(value, list):
        errors.append(f"config file {config_file}: lint.disable must be a list of rule ids, "
                      f"got {value!r}; ignored")
        return ()
    out = []
    for rid in value:
        if isinstance(rid, str) and rid in lint.RULES:
            if rid not in out:
                out.append(rid)
        else:
            errors.append(f"config file {config_file}: lint.disable: unknown rule {rid!r} ignored "
                          f"(rules: {', '.join(lint.RULES)})")
    return tuple(out)


def _merge_live2d(data, into, legacy_additive, config_file, errors):
    """Overlay ``data["live2d"]`` onto ``into`` (``DEFAULT_LIVE2D``'s keys).
    ``legacy_additive`` (``lint.additive_mode``, F003) applies unless
    ``live2d.additive_mode`` is given, with a note either way."""
    given = data.get("live2d", {})
    if not isinstance(given, dict):
        errors.append(f"config file {config_file}: 'live2d' must be an object; ignored")
        given = {}
    additive_key = "live2d.additive_mode"
    if legacy_additive is not None:
        if "additive_mode" in given:
            errors.append(f"config file {config_file}: lint.additive_mode ignored: it moved to "
                          "live2d.additive_mode, which is also set (F004)")
        else:
            errors.append(f"config file {config_file}: lint.additive_mode moved to "
                          "live2d.additive_mode (F004); read from the old place for now")
            given = {**given, "additive_mode": legacy_additive}
            additive_key = "lint.additive_mode"
    for key, value in given.items():
        if key not in into:
            errors.append(f"config file {config_file}: unknown key live2d.{key} ignored")
        elif key == "enabled":
            if isinstance(value, bool):
                into[key] = value
            else:
                errors.append(f"config file {config_file}: live2d.enabled must be true or false, "
                              f"got {value!r}; default {into[key]} kept")
        elif key == "additive_mode":
            if isinstance(value, str) and value in LIVE2D_ADDITIVE_MODES:
                into[key] = value
            else:
                choices = ", ".join(repr(m) for m in LIVE2D_ADDITIVE_MODES)
                errors.append(f"config file {config_file}: {additive_key} must be one of {choices}, "
                              f"got {value!r}; default {into[key]!r} kept")
        elif key == "allowed_blend_modes":
            from . import blendmodes  # pure
            if isinstance(value, list) and value and all(isinstance(m, str) and blendmodes.is_valid(m)
                                                         for m in value):
                into[key] = tuple(dict.fromkeys(value))
            else:
                errors.append(f"config file {config_file}: live2d.allowed_blend_modes must be a non-empty "
                              f"list of Krita blend mode ids (such as \"normal\", \"multiply\", \"add\"), "
                              f"got {value!r}; default kept")
        elif key == "max_nesting":
            if type(value) is int and 1 <= value <= LIVE2D_MAX_NESTING:
                into[key] = value
            else:
                errors.append(f"config file {config_file}: live2d.max_nesting must be an integer from 1 "
                              f"to {LIVE2D_MAX_NESTING}, got {value!r}; default {into[key]} kept")
        elif key == "pose_pattern":
            problem = _regex_problem(value, groups=("stem", "pose"))
            if problem:
                errors.append(f"config file {config_file}: live2d.pose_pattern {problem}, "
                              f"got {value!r}; default kept")
            else:
                into[key] = value
        elif key == "default_pose":
            if isinstance(value, str) and value.strip() == value and value:
                into[key] = value
            else:
                errors.append(f"config file {config_file}: live2d.default_pose must be a non-empty "
                              f"string, got {value!r}; default {into[key]!r} kept")


# How config changes take effect (/health's config_reload; K2).
RELOAD_NOTE = ("automatic: the plugin re-reads the config file when it changes, from the "
               "next call on; no restart needed")


def describe(settings):
    """The config fields of ``/health`` and ``reload_config`` (K2): the
    resolved ``allowed_dirs`` (the output dir first), the config file, the
    key that lists extra folders, whether the file exists and was used,
    how changes apply, limits, lint settings and problems."""
    return {
        "allowed_dirs": list(settings.roots),
        "config_file": settings.config_file,
        "config_exists": os.path.isfile(settings.config_file),
        "config_loaded": settings.config_loaded,
        "config_key": ALLOWED_DIRS_KEY,
        "config_dirs": list(settings.file_dirs),  # the file's entries, as written
        "allowed_dirs_env": ALLOWED_DIRS_ENV,
        "config_reload": RELOAD_NOTE,
        "limits": dict(settings.limits),
        "lint": plain(settings.lint),  # krita_lint's settings (D14; F004)
        "live2d": plain(settings.live2d),  # the Live2D conventions (F004; off by default)
        "config_errors": list(settings.errors),
    }


def plain(mapping):
    """``mapping`` as a JSON-ready dict: tuples become lists."""
    return {k: list(v) if isinstance(v, tuple) else v for k, v in mapping.items()}


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
