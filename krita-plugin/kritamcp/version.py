"""Plugin and wire-protocol versions. Pure: no krita / PyQt5 imports."""

# Wire protocol between server.py and the plugin. 2 = the D2 envelope
# ({"ok", "result" | "error", "elapsed_ms"}). server.py refuses to talk to a
# plugin reporting a different number. Bump it on any incompatible change.
# 3 (F003): new parameters the MCP tools send by default (list_layers limit,
# export_layers verbose, lint group_by, diff_documents ignore_alpha0_rgb, …),
# which a protocol-2 plugin would ignore or refuse.
# F004 keeps 3: no parameter or result field the server relies on changed
# shape (lint's new rules_off and the notes lists are optional extras).
PROTOCOL = 3

# 0.4.0 (F004): the Live2D conventions are opt-in (config live2d.enabled),
# so lint's default rule set and set_canvas_size's warnings differ.
PLUGIN_VERSION = "0.4.0"

# Krita's version string, cached by the extension at startup (createActions,
# main thread) so /health can report it from the HTTP thread without calling
# libkis. None until then, and always None headless.
KRITA_VERSION = None
