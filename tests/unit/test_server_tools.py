"""Smoke test: server.py imports without Krita and exposes its tools."""

import asyncio

import server

EXPECTED_TOOLS = {
    "krita_health",
    "krita_new_canvas",
    "krita_set_color",
    "krita_set_brush",
    "krita_stroke",
    "krita_fill",
    "krita_draw_shape",
    "krita_get_canvas",
    "krita_undo",
    "krita_redo",
    "krita_clear",
    "krita_save",
    "krita_get_color_at",
    "krita_list_brushes",
    "krita_open_file",
}


def test_server_registers_expected_tools():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_tool_functions_are_callable():
    for name in EXPECTED_TOOLS:
        assert callable(getattr(server, name)), name
