"""The pure plugin modules import without Krita or PyQt5.

Importing a submodule runs kritamcp/__init__.py, which registers the
extension only when `krita` is importable. Here it isn't, so nothing is
registered and only the pure modules get loaded.
"""

import importlib
import os
import subprocess
import sys

import pytest

PURE = ["kritamcp.config", "kritamcp.errors", "kritamcp.paths", "kritamcp.params",
        "kritamcp.refs", "kritamcp.version"]


@pytest.mark.parametrize("name", PURE)
def test_pure_module_imports(name):
    importlib.import_module(name)


def test_package_import_has_no_krita_side_effects():
    # Fresh interpreter, so earlier imports in this session can't hide anything.
    code = (
        "import sys, kritamcp, kritamcp.config, kritamcp.errors, kritamcp.paths, kritamcp.params\n"
        "import kritamcp.refs, kritamcp.version\n"
        "assert kritamcp.Krita is None\n"
        "assert not hasattr(kritamcp, 'KritaMCPExtension')\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('krita', 'PyQt5')"
        " or m in ('kritamcp.bridge', 'kritamcp.extension', 'kritamcp.dispatch'))\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=_plugin_dir())


def test_config_values_unchanged():
    from kritamcp import config
    assert config.SERVER_PORT == 5678
    assert config.TOKEN_HEADER == "X-Krita-MCP-Token"
    assert config.ALLOWED_HOSTS == {"localhost:5678", "127.0.0.1:5678"}
    assert config.COORD == 10 * 16384
    assert (config.MAX_BODY_BYTES, config.MAX_BRUSH_SIZE, config.MAX_STROKE_POINTS,
            config.MAX_LIMIT) == (5 * 1024 * 1024, 1000, 10000, 1000)


def test_old_static_limits_are_gone():
    # The canvas limits are config.current().limits now (D8); no module
    # constant may keep the old 8192 / 16.8 MP values alive.
    from kritamcp import config
    for name in ("ALLOWED_DIRS", "MAX_CANVAS_DIM", "MAX_CANVAS_PIXELS"):
        assert not hasattr(config, name), name


def _plugin_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "krita-plugin")
