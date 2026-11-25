"""The queue between the HTTP threads and Krita's main thread (D9).

Pure: no krita / PyQt5 imports, so it is unit-tested in the repo venv.

HTTP threads call ``submit`` and then ``wait``; the main thread's timer
calls ``next``, runs the command, and calls ``finish``. Commands run one at
a time, in order.

A ``wait`` that times out abandons its command. If the command hasn't
started, it is removed from the queue and never runs. If it is running,
its result is dropped when it finishes (Krita may still apply it). So no
result is kept that nobody will collect.
"""

import collections
import math
import threading
import time

from .errors import CommandError

# Upper bound for a caller's ``timeout_s`` (D9).
MAX_TIMEOUT_S = 900


def pop_timeout(params, default):
    """Remove ``timeout_s`` from ``params`` and return ``(timeout, params)``.

    ``params`` is not modified; the returned dict lacks ``timeout_s``.
    Absent or null means ``default``. Otherwise it must be a finite number
    with 0 < t <= MAX_TIMEOUT_S, else ``CommandError("bad_request")``.
    """
    if "timeout_s" not in params:
        return default, params
    rest = {k: v for k, v in params.items() if k != "timeout_s"}
    value = params["timeout_s"]
    if value is None:
        return default, rest
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT_S):
        raise CommandError("bad_request", f"'timeout_s' must be a number of seconds, "
                                          f"more than 0 and at most {MAX_TIMEOUT_S}")
    return float(value), rest


def _action_of(command):
    action = command.get("action") if isinstance(command, dict) else None
    return action if isinstance(action, str) else None


class CommandQueue:
    """``clock`` (seconds, monotonic) times the busy command for ``status``."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._cond = threading.Condition()
        self._counter = 0
        self._queue = collections.deque()  # (id, command), not yet started
        self._waiting = {}                 # id -> action, while someone waits for it
        self._results = {}                 # id -> envelope, ready for its waiter
        self._busy = None                  # (id, action, start) of the running command

    def submit(self, command):
        """Queue ``command`` and return its id (unique, increasing)."""
        with self._cond:
            self._counter += 1
            command_id = self._counter
            self._waiting[command_id] = _action_of(command)
            self._queue.append((command_id, command))
            return command_id

    def wait(self, command_id, timeout):
        """Block until the command's envelope arrives, or ``timeout`` seconds
        pass. On timeout, abandon the command and return a ``timeout``
        envelope whose ``details.started`` says whether it had started.
        """
        with self._cond:
            if self._cond.wait_for(lambda: command_id in self._results, timeout):
                self._waiting.pop(command_id, None)
                return self._results.pop(command_id)
            action = self._waiting.pop(command_id, None)
            started = self._busy is not None and self._busy[0] == command_id
            if started:
                elapsed_ms = self._elapsed_ms()
                message = (f"'{action}' was still running after {timeout:g} s. Its result "
                           f"will be discarded, but Krita may still apply it; check the "
                           f"document before retrying")
            else:
                elapsed_ms = 0
                self._queue = collections.deque(
                    item for item in self._queue if item[0] != command_id)
                busy = f" (Krita is busy with '{self._busy[1]}')" if self._busy else ""
                message = (f"'{action}' did not start within {timeout:g} s{busy}. It has "
                           f"been cancelled and will not run")
            return {"ok": False,
                    "error": {"code": "timeout", "message": message,
                              "details": {"action": action, "timeout_s": timeout,
                                          "started": started}},
                    "elapsed_ms": elapsed_ms}

    def next(self):
        """For the main thread: the next ``(id, command)`` to run, or None.
        Marks it busy until ``finish``. Abandoned commands are skipped.
        """
        with self._cond:
            while self._queue:
                command_id, command = self._queue.popleft()
                if command_id in self._waiting:
                    self._busy = (command_id, _action_of(command), self._clock())
                    return command_id, command
            return None

    def finish(self, command_id, envelope):
        """For the main thread: hand over the envelope of a command from
        ``next``. Dropped if its waiter has given up.
        """
        with self._cond:
            if self._busy is not None and self._busy[0] == command_id:
                self._busy = None
            if command_id in self._waiting:
                self._results[command_id] = envelope
                self._cond.notify_all()

    def status(self):
        """``{"busy": {"action", "elapsed_ms"} | None, "queued": n}``."""
        with self._cond:
            busy = None
            if self._busy is not None:
                busy = {"action": self._busy[1], "elapsed_ms": self._elapsed_ms()}
            return {"busy": busy, "queued": len(self._queue)}

    def _elapsed_ms(self):
        return int(round((self._clock() - self._busy[2]) * 1000))
