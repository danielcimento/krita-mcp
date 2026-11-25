"""kritamcp.cmdqueue with real threads; a fake executor plays Krita's main thread."""

import threading
import time

import pytest

from kritamcp import cmdqueue
from kritamcp.cmdqueue import CommandQueue
from kritamcp.errors import CommandError

OK = {"ok": True, "result": {}, "elapsed_ms": 1}


class Executor(threading.Thread):
    """Drains the queue like extension.process_commands. ``handler(cmd)``
    returns the envelope; ``ran`` records the commands it ran."""

    def __init__(self, queue, handler=None):
        super().__init__(daemon=True)
        self.queue = queue
        self.handler = handler or (lambda cmd: {"ok": True, "result": dict(cmd), "elapsed_ms": 0})
        self.ran = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            item = self.queue.next()
            if item is None:
                time.sleep(0.001)
                continue
            command_id, command = item
            self.ran.append(command)
            self.queue.finish(command_id, self.handler(command))

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.join(5)


def test_ids_unique_and_increasing_under_concurrent_submits():
    q = CommandQueue()
    ids, lock = [], threading.Lock()
    start = threading.Barrier(16)

    def submitter():
        start.wait()
        mine = [q.submit({"action": "undo"}) for _ in range(200)]
        assert mine == sorted(mine)
        with lock:
            ids.extend(mine)

    threads = [threading.Thread(target=submitter) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(ids) == list(range(1, 16 * 200 + 1))
    assert q.status() == {"busy": None, "queued": 16 * 200}


def test_result_delivered():
    q = CommandQueue()
    with Executor(q):
        command_id = q.submit({"action": "fill", "params": {"x": 1}})
        assert q.wait(command_id, 5) == {"ok": True, "result": {"action": "fill",
                                                                "params": {"x": 1}},
                                         "elapsed_ms": 0}
    assert q._results == {} and q._waiting == {}


def test_wait_returns_promptly_when_result_arrives():
    q = CommandQueue()
    command_id = q.submit({"action": "undo"})
    got = {}

    def waiter():
        got["envelope"] = q.wait(command_id, 5)
        got["at"] = time.monotonic()

    t = threading.Thread(target=waiter)
    t.start()
    assert q.next() == (command_id, {"action": "undo"})
    time.sleep(0.05)  # the waiter is blocked by now
    finished = time.monotonic()
    q.finish(command_id, OK)
    t.join(5)
    assert got["envelope"] == OK
    assert got["at"] - finished < 0.05  # no polling interval


def test_timeout_before_start_skips_the_command():
    q = CommandQueue()
    command_id = q.submit({"action": "save"})
    envelope = q.wait(command_id, 0.05)
    assert envelope["ok"] is False
    assert envelope["elapsed_ms"] == 0
    error = envelope["error"]
    assert error["code"] == "timeout"
    assert error["details"] == {"action": "save", "timeout_s": 0.05, "started": False}
    assert "will not run" in error["message"]
    assert q.status() == {"busy": None, "queued": 0}
    called = []
    with Executor(q, handler=lambda cmd: called.append(cmd) or OK) as ex:
        later = q.submit({"action": "undo"})
        assert q.wait(later, 5) == OK
    assert called == [{"action": "undo"}] and ex.ran == called


def test_timeout_names_the_busy_command():
    q = CommandQueue()
    first = q.submit({"action": "save"})
    second = q.submit({"action": "undo"})
    assert q.next()[0] == first
    error = q.wait(second, 0.05)["error"]
    assert error["details"]["started"] is False
    assert "busy with 'save'" in error["message"]
    q.finish(first, OK)
    assert q.next() is None  # `second` was removed
    assert q.wait(first, 1) == OK
    assert q._results == {} and q._waiting == {}


def test_timeout_while_running_drops_the_result():
    q = CommandQueue()
    running, release = threading.Event(), threading.Event()

    def slow(cmd):
        running.set()
        release.wait(5)
        return OK

    with Executor(q, handler=slow):
        command_id = q.submit({"action": "get_canvas"})
        assert running.wait(5)
        envelope = q.wait(command_id, 0.05)
        assert envelope["error"]["code"] == "timeout"
        assert envelope["error"]["details"] == {"action": "get_canvas", "timeout_s": 0.05,
                                                "started": True}
        assert "discarded" in envelope["error"]["message"]
        assert envelope["elapsed_ms"] >= 40
        release.set()
        # The executor finishes it; nothing is kept.
        follow = q.submit({"action": "undo"})
        assert q.wait(follow, 5)["ok"] is True
    assert q._results == {} and q._waiting == {}


def test_status_reports_busy_and_queued():
    now = [100.0]
    q = CommandQueue(clock=lambda: now[0])
    a = q.submit({"action": "save"})
    q.submit({"action": "undo"})
    q.submit({"action": "redo"})
    assert q.status() == {"busy": None, "queued": 3}
    assert q.next() == (a, {"action": "save"})
    now[0] += 12.3456
    assert q.status() == {"busy": {"action": "save", "elapsed_ms": 12346}, "queued": 2}
    q.finish(a, OK)
    assert q.status() == {"busy": None, "queued": 2}


def test_status_from_another_thread_while_running():
    q = CommandQueue()
    running, release = threading.Event(), threading.Event()

    def slow(cmd):
        running.set()
        release.wait(5)
        return OK

    with Executor(q, handler=slow):
        first = q.submit({"action": "save"})
        assert running.wait(5)
        q.submit({"action": "undo"})
        seen = {}
        t = threading.Thread(target=lambda: seen.update(q.status()))
        t.start()
        t.join(5)
        assert seen["busy"]["action"] == "save" and seen["queued"] == 1
        release.set()
        assert q.wait(first, 5) == OK


def test_many_waiters_get_their_own_results():
    q = CommandQueue()
    n = 40
    results = {}

    def handler(cmd):
        time.sleep(0.001 * (cmd["params"]["i"] % 3))
        return {"ok": True, "result": {"i": cmd["params"]["i"]}, "elapsed_ms": 0}

    def client(i):
        command_id = q.submit({"action": "x", "params": {"i": i}})
        results[i] = q.wait(command_id, 10)

    with Executor(q, handler=handler):
        threads = [threading.Thread(target=client, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
    assert {i: r["result"]["i"] for i, r in results.items()} == {i: i for i in range(n)}
    assert q._results == {} and q._waiting == {}


def test_non_dict_command_has_no_action():
    q = CommandQueue()
    command_id = q.submit(["not", "a", "dict"])
    assert q.next() == (command_id, ["not", "a", "dict"])
    assert q.status()["busy"]["action"] is None


@pytest.mark.parametrize("params,expected", [
    ({}, (30, {})),
    ({"x": 1}, (30, {"x": 1})),
    ({"timeout_s": None, "x": 1}, (30, {"x": 1})),
    ({"timeout_s": 0.2}, (0.2, {})),
    ({"timeout_s": 900, "x": 1}, (900.0, {"x": 1})),
])
def test_pop_timeout(params, expected):
    original = dict(params)
    assert cmdqueue.pop_timeout(params, 30) == expected
    assert params == original


@pytest.mark.parametrize("value", [0, -1, 900.5, 901, "x", float("nan"), float("inf"),
                                   True, [5]])
def test_pop_timeout_rejects(value):
    with pytest.raises(CommandError) as exc:
        cmdqueue.pop_timeout({"timeout_s": value}, 30)
    assert exc.value.code == "bad_request"
    assert "timeout_s" in exc.value.message
