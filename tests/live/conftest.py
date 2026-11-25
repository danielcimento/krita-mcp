"""Live tests run against the GUI Krita with this checkout's plugin loaded.

Everything under tests/live is skipped unless KRITA_MCP_LIVE=1.

They only touch what they create:

- Files go into a scratch dir, ``<output dir>/f001-acceptance/<run-id>/``
  (``scratch``), removed at the end of the session unless
  ``KRITA_MCP_KEEP_SCRATCH=1``.
- Documents a test opens are listed in ``docs_to_close`` and closed with
  ``discard=True`` afterwards, whatever happened. Other documents are never
  touched.
- ``config_override`` replaces the plugin's config file
  (``~/.krita-mcp-config.json``, or whatever ``krita_health`` reports as
  ``config_file``) for one test and restores it exactly (content, mode and
  mtime, or its absence) in ``finally``, which also runs on Ctrl-C; an
  ``atexit`` hook is the fallback. A copy of the original is kept next to
  the scratch dir until it has been restored, for recovery by hand if the
  process is killed.
"""

import atexit
import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

LIVE = os.environ.get("KRITA_MCP_LIVE") == "1"
_HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = os.path.expanduser("~/.krita-mcp-config.json")
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")
SCRATCH_PARENT = "f001-acceptance"


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="live test: set KRITA_MCP_LIVE=1 (needs a running Krita)")
    for item in items:
        if _HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.live)
            if not LIVE:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def krita_url():
    return os.environ.get("KRITA_URL", "http://localhost:5678").rstrip("/")


# --- calling server.py's tools --------------------------------------------

class Caller:
    """Calls server.py's tool functions, as an MCP client would.

    ``call("new_canvas", width=8)`` or ``call("krita_new_canvas", ...)``;
    ``call.error("too_large", "new_canvas", ...)`` expects a ToolError with
    that code and returns its message.
    """

    def __init__(self):
        import server  # imported lazily: collection stays offline
        self._server = server

    def _tool(self, tool):
        return getattr(self._server, tool if tool.startswith("krita_") else f"krita_{tool}")

    # Positional-only, so tool parameters such as new_canvas's ``name`` can
    # be passed as keywords.
    def __call__(self, tool, /, **kwargs):
        return self._tool(tool)(**kwargs)

    def error(self, code, tool, /, **kwargs):
        with pytest.raises(ToolError) as info:
            self._tool(tool)(**kwargs)
        message = str(info.value)
        assert message.startswith(f"{code}: "), message
        return message

    def health(self):
        result = self._server.krita_health()
        assert result["running"], result["summary"]
        assert result["protocol_ok"], result["summary"]
        return result


@pytest.fixture(scope="session")
def call():
    return Caller()


@pytest.fixture(scope="session")
def output_dir(call):
    """The plugin's output dir (the first allowed root), realpath'd."""
    dirs = call.health().get("allowed_dirs") or [DEFAULT_OUTPUT_DIR]
    return os.path.realpath(dirs[0])


@pytest.fixture(scope="session")
def run_id():
    return time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"


@pytest.fixture(scope="session")
def scratch(output_dir, run_id):
    """``(absolute dir, path relative to the output dir)`` of this run's
    scratch dir, inside the output dir (so it is always allowed)."""
    rel = os.path.join(SCRATCH_PARENT, run_id)
    path = os.path.join(output_dir, rel)
    os.makedirs(path, exist_ok=True)
    try:
        yield path, rel
    finally:
        if os.environ.get("KRITA_MCP_KEEP_SCRATCH") != "1":
            shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def docs_to_close(call):
    """A list the test appends document handles to; each is closed with
    discard=True afterwards (already closed: ignored)."""
    handles = []
    try:
        yield handles
    finally:
        for handle in handles:
            try:
                call("close_document", doc=handle, discard=True)
            except ToolError as e:
                if not str(e).startswith("not_found: "):
                    raise


# --- the plugin's config file ------------------------------------------------

class ConfigOverride:
    """Backs up the plugin's config file exactly and restores it once.

    ``original`` is the parsed original (``{}`` if absent or not JSON), for
    tests that want to keep the user's own roots while overriding.
    """

    def __init__(self, path, backup_dir):
        self.path = path
        if os.path.islink(path):
            pytest.skip(f"{path} is a symlink; not overriding it")
        self._restored = False
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self._saved = None
        else:
            if not stat.S_ISREG(st.st_mode):
                pytest.skip(f"{path} is not a regular file; not overriding it")
            with open(path, "rb") as f:
                content = f.read()
            self._saved = (content, stat.S_IMODE(st.st_mode), st.st_atime_ns, st.st_mtime_ns)
        self.original = {}
        if self._saved is not None:
            try:
                parsed = json.loads(self._saved[0])
                self.original = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                pass
        # A copy for recovery by hand if this process is killed.
        self._backup = None
        if self._saved is not None:
            os.makedirs(backup_dir, exist_ok=True)
            self._backup = os.path.join(backup_dir, "config-backup-%d.json" % os.getpid())
            fd = os.open(self._backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(self._saved[0])
        atexit.register(self.restore)

    def write(self, data, mode=0o600):
        """Replace the file's content with ``data`` (JSON), mode ``mode``."""
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        if mode != 0o600:
            os.chmod(self.path, mode)

    def chmod(self, mode):
        os.chmod(self.path, mode)

    def restore(self):
        """Put the original back (or remove the file if there was none).
        Idempotent."""
        if self._restored:
            return
        if self._saved is None:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
        else:
            content, mode, atime, mtime = self._saved
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.chmod(self.path, mode)
            os.utime(self.path, ns=(atime, mtime))
            if self._backup:
                os.unlink(self._backup)
        self._restored = True
        atexit.unregister(self.restore)


@pytest.fixture
def config_override(call, output_dir):
    """Override the plugin's config file for one test; restored in teardown
    no matter what (KeyboardInterrupt included)."""
    path = call.health().get("config_file") or DEFAULT_CONFIG_FILE
    override = ConfigOverride(path, os.path.join(output_dir, SCRATCH_PARENT))
    try:
        yield override
    finally:
        override.restore()
