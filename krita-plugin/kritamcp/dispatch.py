"""Command registry and dispatch (D2, D9, D12). Called on Krita's main thread.

Handlers live in ``commands/`` and register themselves with ``@command``::

    @command("fill", mutating=True)
    def cmd_fill(params):
        ...
        return {"x": x, "y": y}          # the payload
        raise CommandError("not_found", "No active layer")

``execute`` wraps the payload, or the error, in the response envelope:

    {"ok": True,  "result": {...}, "elapsed_ms": 12}
    {"ok": False, "error": {"code", "message", "details"?}, "elapsed_ms": 3}

The registry also records each command's default timeout and whether it
mutates documents. The bridge reads them through ``get`` (from its HTTP
threads: a plain dict read) to decide how long a request waits, and lists
them in ``/info`` so server.py can set its transport timeout (D9).
"""

import time
import traceback
from collections import namedtuple

from .errors import CommandError

# Default timeouts in seconds (D9).
QUERY_TIMEOUT = 30
LONG_TIMEOUT = 300

Command = namedtuple("Command", "name fn timeout mutating")

_registry = {}


def command(name, timeout=QUERY_TIMEOUT, mutating=False):
    """Register ``fn(params) -> dict`` as the handler for action ``name``.

    ``mutating``: the command changes a document or the set of open
    documents (what the journal and batch rollback will care about). UI
    state such as the foreground color or brush preset does not count.
    """
    def register(fn):
        if name in _registry:
            raise ValueError(f"command {name!r} registered twice")
        _registry[name] = Command(name, fn, timeout, mutating)
        return fn
    return register


def get(name):
    """The registered ``Command`` for ``name``, or None."""
    return _registry.get(name)


def actions():
    """Sorted list of registered action names."""
    return sorted(_registry)


def _run(command):
    if not isinstance(command, dict):
        raise CommandError("bad_request", "Command must be a JSON object")
    action = command.get("action")
    entry = _registry.get(action) if isinstance(action, str) else None
    if entry is None:
        raise CommandError("bad_request", f"Unknown action: {action}")
    params = command.get("params", {})
    if not isinstance(params, dict):
        raise CommandError("bad_request", "'params' must be a JSON object")
    result = entry.fn(params)
    if not isinstance(result, dict):
        raise TypeError(f"handler for {action!r} returned {type(result).__name__}, not dict")
    return result


def execute(command):
    """Run one command and return its envelope. Never raises."""
    start = time.perf_counter()
    try:
        envelope = {"ok": True, "result": _run(command)}
    except CommandError as e:
        envelope = {"ok": False, "error": e.to_dict()}
    except Exception as e:
        # Unexpected: report it as krita_error and log the traceback to
        # Krita's log so it can be debugged.
        action = command.get("action") if isinstance(command, dict) else None
        print(f"[KritaMCP] command {action!r} failed:")
        traceback.print_exc()
        envelope = {"ok": False, "error": {"code": "krita_error",
                                           "message": str(e) or type(e).__name__}}
    envelope["elapsed_ms"] = int(round((time.perf_counter() - start) * 1000))
    return envelope


# Importing the command modules fills the registry. At the bottom because
# they import ``command`` from this module.
from .commands import documents, history, layers, paint, pixels  # noqa: E402,F401
