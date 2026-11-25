"""Plugin and wire-protocol versions. Pure: no krita / PyQt5 imports."""

# Wire protocol between server.py and the plugin. 2 = the D2 envelope
# ({"ok", "result" | "error", "elapsed_ms"}). server.py refuses to talk to a
# plugin reporting a different number. Bump it on any incompatible change.
PROTOCOL = 2

PLUGIN_VERSION = "0.2.0"

# Krita's version string, cached by the extension at startup (createActions,
# main thread) so /health can report it from the HTTP thread without calling
# libkis. None until then, and always None headless.
KRITA_VERSION = None
