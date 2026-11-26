"""The MCP server's own version and source commit (F005 K15).

``krita_health`` reports them next to the plugin's version, so a report
can say which server code ran. The commit is read once per process from
the git checkout ``server.py`` runs from (the code that is loaded), and
is None when that isn't a git checkout or git isn't available.
"""

import os
import subprocess
import threading

# Released together with the plugin from one checkout; kept equal to
# krita-plugin/kritamcp/version.py PLUGIN_VERSION.
SERVER_VERSION = "0.5.0"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.Lock()
_cached = None


def _git(*args):
    try:
        done = subprocess.run(["git", "-C", _ROOT, *args], capture_output=True, text=True,
                              timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def read_commit():
    """``(commit, dirty)``: the checkout's HEAD (full SHA) and whether it
    has uncommitted changes (untracked files don't count), or ``(None,
    None)`` outside a git checkout."""
    head = _git("rev-parse", "HEAD")
    if not head or not head.strip():
        return None, None
    status = _git("status", "--porcelain", "--untracked-files=no")
    return head.strip(), None if status is None else bool(status.strip())


def server_info():
    """``{server_version, server_commit, server_dirty}``, the commit read
    on the first call and cached for the process."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = read_commit()
    commit, dirty = _cached
    return {"server_version": SERVER_VERSION, "server_commit": commit, "server_dirty": dirty}
