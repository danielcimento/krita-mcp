"""
Krita MCP Bridge - HTTP server for external paint commands in Krita
Allows Claude (or any MCP client) to paint by sending commands to this plugin.

Importing this package only registers the extension with Krita. The token,
output directory, HTTP server and command timer are set up when Krita calls
the extension's createActions() (GUI only; never under kritarunner).

Outside Krita (pytest in the repo venv) `krita` is not importable, so
nothing is registered and the pure submodules (config, errors, paths,
params) can be imported on their own.
"""

try:
    from krita import Krita
except ImportError:
    Krita = None

if Krita is not None:
    from .extension import KritaMCPExtension

    # Register the extension
    Krita.instance().addExtension(KritaMCPExtension(Krita.instance()))
