"""Headless test runner shim for kritarunner (D16).

``tools/install_test_runner.sh`` symlinks this file into kritarunner's own
profile (``~/Library/Application Support/kritarunner/pykrita/``), the only
place kritarunner imports modules from. ``make test-krita`` then runs::

    kritarunner -s kmcp_test_runner -f run <repo> [pattern ...]

``run(args)``:

- ``args[0]``: repo checkout path. Falls back to the real location of this
  file (it is a symlink in the runner profile).
- ``args[1:]``: optional test name patterns, matched like ``unittest -k``
  against ``module.Class.method`` (a plain string is a substring match,
  ``*`` and ``?`` are wildcards).

It puts ``<repo>/krita-plugin`` and ``<repo>/tests/krita`` on ``sys.path``,
discovers ``tests/krita/test_*.py``, runs them with ``unittest``, writes a
JUnit-style report and the test output into
``~/krita-mcp-output/test-krita/``, and prints one marker line::

    KMCP_TEST_RESULT: PASS ran=3 failures=0 errors=0 skipped=1
    KMCP_TEST_RESULT: FAIL ran=3 failures=1 errors=0 skipped=1

kritarunner's exit code does not reflect the outcome, so callers must read
the marker. ``run`` never raises: any runner error is printed and reported
as FAIL.

Runs on Krita's Python (3.13); stdlib only.
"""

import datetime
import io
import os
import platform
import socket
import sys
import time
import traceback
import unittest
import xml.etree.ElementTree as ET

MARKER = "KMCP_TEST_RESULT"
OUT_DIR = os.path.expanduser("~/krita-mcp-output/test-krita")
REPORT_PATH = os.path.join(OUT_DIR, "report.xml")
OUTPUT_PATH = os.path.join(OUT_DIR, "output.txt")


class _Tee(io.TextIOBase):
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for stream in self._streams:
            stream.write(s)
        return len(s)

    def flush(self):
        for stream in self._streams:
            stream.flush()


class _RecordingResult(unittest.TextTestResult):
    """TextTestResult that also keeps per-test records for the report."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self._started = {}

    def startTest(self, test):
        self._started[test.id()] = time.perf_counter()
        super().startTest(test)

    def _record(self, test, status, message="", text=""):
        start = self._started.pop(test.id(), None)
        elapsed = time.perf_counter() - start if start is not None else 0.0
        test_id = test.id()
        if "." in test_id:
            classname, name = test_id.rsplit(".", 1)
        else:  # e.g. a setUpModule error holder
            classname, name = test_id, test_id
        self.records.append({"classname": classname, "name": name, "time": elapsed,
                             "status": status, "message": message, "text": text})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failure", _err_message(err), self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", _err_message(err), self.errors[-1][1])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "passed", "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "failure", "unexpected success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            status = "failure" if issubclass(err[0], test.failureException) else "error"
            text = (self.failures if status == "failure" else self.errors)[-1][1]
            self._started.setdefault(subtest.id(), time.perf_counter())
            self._record(subtest, status, _err_message(err), text)


def _err_message(err):
    exc_type, exc, _ = err
    return "%s: %s" % (exc_type.__name__, exc)


def _counts(records):
    counts = {"failure": 0, "error": 0, "skipped": 0}
    for r in records:
        if r["status"] in counts:
            counts[r["status"]] += 1
    return counts


def _write_report(records, total_time, extra_error=None):
    if extra_error is not None:
        records = records + [{"classname": "kmcp_test_runner", "name": "runner",
                              "time": 0.0, "status": "error",
                              "message": extra_error.splitlines()[-1] if extra_error else "",
                              "text": extra_error}]
    counts = _counts(records)
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite", {
        "name": "krita-headless",
        "tests": str(len(records)),
        "failures": str(counts["failure"]),
        "errors": str(counts["error"]),
        "skipped": str(counts["skipped"]),
        "time": "%.3f" % total_time,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
    })
    props = ET.SubElement(suite, "properties")
    for name, value in _environment().items():
        ET.SubElement(props, "property", {"name": name, "value": value})
    for r in records:
        case = ET.SubElement(suite, "testcase", {
            "classname": r["classname"], "name": r["name"], "time": "%.3f" % r["time"]})
        if r["status"] in ("failure", "error"):
            el = ET.SubElement(case, r["status"], {"message": r["message"]})
            el.text = r["text"]
        elif r["status"] == "skipped":
            ET.SubElement(case, "skipped", {"message": r["message"]})
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = REPORT_PATH + ".tmp"
    ET.ElementTree(suites).write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, REPORT_PATH)


def _environment():
    env = {"python": platform.python_version()}
    try:
        from krita import Krita
        env["krita"] = Krita.instance().version()
    except Exception:  # report even if krita is unavailable
        env["krita"] = "unavailable"
    return env


def _resolve_repo(args):
    if args and args[0]:
        return os.path.abspath(os.path.expanduser(args[0]))
    # This file is symlinked into the runner profile: <repo>/tests/krita/<this>.
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def _remove_stale_outputs():
    for path in (REPORT_PATH, OUTPUT_PATH):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _run(args):
    repo = _resolve_repo(args)
    patterns = [p for p in args[1:] if p]
    tests_dir = os.path.join(repo, "tests", "krita")
    plugin_dir = os.path.join(repo, "krita-plugin")
    if not os.path.isdir(tests_dir):
        raise RuntimeError("no tests/krita in repo path %r (pass the repo as the first argument)" % repo)
    for path in (plugin_dir, tests_dir):
        if path not in sys.path:
            sys.path.insert(0, path)

    loader = unittest.TestLoader()
    if patterns:
        loader.testNamePatterns = [p if ("*" in p or "?" in p) else "*%s*" % p for p in patterns]
    suite = loader.discover(start_dir=tests_dir, pattern="test_*.py", top_level_dir=tests_dir)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as out:
        stream = _Tee(sys.stdout, out)
        stream.write("kmcp_test_runner: repo=%s patterns=%s\n" % (repo, patterns or "all"))
        runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=_RecordingResult)
        start = time.perf_counter()
        result = runner.run(suite)
        total = time.perf_counter() - start
        stream.flush()

    _write_report(result.records, total)
    ok = result.wasSuccessful() and result.testsRun > 0
    summary = "ran=%d failures=%d errors=%d skipped=%d" % (
        result.testsRun, len(result.failures) + len(result.unexpectedSuccesses),
        len(result.errors), len(result.skipped))
    if result.testsRun == 0:
        summary += " (no tests ran)"
    return ok, summary


def run(args=None):
    """kritarunner entry point. Never raises."""
    ok, summary = False, "runner error"
    try:
        args = [str(a) for a in (args or [])]
        try:
            _remove_stale_outputs()
        except Exception:
            pass
        ok, summary = _run(args)
    except BaseException:
        tb = traceback.format_exc()
        sys.stdout.write(tb)
        try:
            _write_report([], 0.0, extra_error=tb)
        except Exception:
            traceback.print_exc(file=sys.stdout)
    try:
        sys.stdout.write("%s: %s %s report=%s\n" % (MARKER, "PASS" if ok else "FAIL", summary, REPORT_PATH))
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    return 0 if ok else 1
