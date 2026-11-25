"""server.py's tools, with client.call / client.get_health mocked (no Krita)."""

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server
from krita_mcp_server import client

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

# tool -> (kwargs, plugin action, canned plugin payload)
CASES = {
    "krita_new_canvas": ({"width": 64, "height": 48}, "new_canvas",
                         {"width": 64, "height": 48, "name": "New Canvas"}),
    "krita_set_color": ({"color": "#ff0000"}, "set_color", {"color": "#ff0000"}),
    "krita_set_brush": ({"preset": "Basic", "size": 5}, "set_brush",
                        {"preset": "Basic", "size": 5, "opacity": None}),
    "krita_stroke": ({"points": [[0, 0], [5, 5]]}, "stroke", {"points_count": 2, "hardness": 0.5}),
    "krita_fill": ({"x": 1, "y": 2, "radius": 3}, "fill", {"x": 1, "y": 2, "radius": 3}),
    "krita_draw_shape": ({"shape": "rectangle", "x": 0, "y": 0}, "draw_shape",
                         {"shape": "rectangle"}),
    "krita_get_canvas": ({}, "get_canvas", {"path": "/out/canvas.png"}),
    "krita_undo": ({}, "undo", {}),
    "krita_redo": ({}, "redo", {}),
    "krita_clear": ({}, "clear", {"color": "#1a1a2e"}),
    "krita_save": ({"path": "a.png"}, "save", {"path": "/out/a.png"}),
    "krita_get_color_at": ({"x": 1, "y": 1}, "get_color_at",
                           {"color": "#010203", "r": 1, "g": 2, "b": 3, "a": 255}),
    "krita_list_brushes": ({}, "list_brushes", {"brushes": ["b) Basic-1"], "count": 1}),
    "krita_open_file": ({"path": "a.kra"}, "open_file",
                        {"path": "/out/a.kra", "name": "a", "width": 4, "height": 4}),
}


class FakeCall:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.calls = []

    def __call__(self, action, params=None, timeout=30.0):
        self.calls.append((action, params, timeout))
        return {**self.payload, "elapsed_ms": 4}


@pytest.fixture
def fake_call(monkeypatch):
    fake = FakeCall()
    monkeypatch.setattr(client, "call", fake)
    return fake


def test_server_registers_expected_tools():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_tool_functions_are_callable():
    # FastMCP 4.2.6's @mcp.tool() leaves the plain function in place.
    for name in EXPECTED_TOOLS:
        assert callable(getattr(server, name)), name


def test_every_tool_except_health_has_a_case():
    assert set(CASES) == EXPECTED_TOOLS - {"krita_health"}


@pytest.mark.parametrize("name", sorted(CASES))
def test_tool_returns_payload_with_summary(fake_call, name):
    kwargs, action, payload = CASES[name]
    fake_call.payload = payload
    result = getattr(server, name)(**kwargs)
    assert isinstance(result, dict)
    assert isinstance(result["summary"], str) and result["summary"].strip()
    assert "\n" not in result["summary"]
    assert result["elapsed_ms"] == 4
    for key, value in payload.items():
        assert result[key] == value
    assert [c[0] for c in fake_call.calls] == [action]


def test_slow_tools_keep_long_timeout(fake_call):
    server.krita_get_canvas()
    server.krita_save(path="a.png")
    assert [c[2] for c in fake_call.calls] == [120.0, 120.0]


def test_set_brush_sends_size_zero(fake_call):
    server.krita_set_brush(size=0)
    assert fake_call.calls == [("set_brush", {"size": 0}, 30.0)]


def test_set_brush_omits_unset_fields(fake_call):
    server.krita_set_brush(opacity=0.0)
    assert fake_call.calls == [("set_brush", {"opacity": 0.0}, 30.0)]


def test_stroke_with_one_point_raises_bad_request(fake_call):
    with pytest.raises(ToolError) as exc:
        server.krita_stroke(points=[[1, 1]])
    assert str(exc.value).startswith("bad_request: ")
    assert fake_call.calls == []


def test_tool_error_propagates(monkeypatch):
    def failing(action, params=None, timeout=30.0):
        raise ToolError("not_found: No active layer")
    monkeypatch.setattr(client, "call", failing)
    with pytest.raises(ToolError, match="^not_found: No active layer$"):
        server.krita_fill(x=1, y=1)


def test_mcp_client_sees_structured_result_and_is_error(monkeypatch, fake_call):
    """Through FastMCP: dicts become structured content, ToolError an error result."""
    fake_call.payload = {"color": "#ff0000"}

    async def go():
        async with Client(server.mcp) as c:
            ok = await c.call_tool("krita_set_color", {"color": "#ff0000"})
            bad = await c.call_tool("krita_stroke", {"points": [[1, 1]]}, raise_on_error=False)
            return ok, bad

    ok, bad = asyncio.run(go())
    assert not ok.is_error
    assert ok.structured_content == {"summary": "Color set to #ff0000", "color": "#ff0000",
                                     "elapsed_ms": 4}
    assert bad.is_error
    assert bad.content[0].text.startswith("bad_request: ")


def test_health_unreachable_does_not_raise(monkeypatch):
    def unreachable(timeout=None):
        raise ToolError(client.UNAVAILABLE)
    monkeypatch.setattr(client, "get_health", unreachable)
    result = server.krita_health()
    assert result["running"] is False
    assert result["summary"].startswith("Cannot connect")


def test_health_ok(monkeypatch):
    health = {"status": "ok", "plugin": "kritamcp", "protocol": 2,
              "version": "0.2.0", "krita_version": "5.3.4"}
    monkeypatch.setattr(client, "get_health", lambda timeout=None: dict(health))
    result = server.krita_health()
    assert result["running"] is True and result["protocol_ok"] is True
    for key, value in health.items():
        assert result[key] == value
    assert "5.3.4" in result["summary"] and "MISMATCH" not in result["summary"]


def test_health_flags_protocol_mismatch(monkeypatch):
    monkeypatch.setattr(client, "get_health",
                        lambda timeout=None: {"status": "ok", "plugin": "kritamcp"})
    result = server.krita_health()
    assert result["running"] is True and result["protocol_ok"] is False
    assert "MISMATCH" in result["summary"] and "restart Krita" in result["summary"]


def test_health_reports_roots_and_config_errors(monkeypatch):
    health = {"status": "ok", "plugin": "kritamcp", "protocol": 2, "version": "0.2.0",
              "krita_version": "5.3.4", "allowed_dirs": ["/out", "/art"],
              "config_file": "/home/u/.krita-mcp-config.json", "config_loaded": True,
              "limits": {"max_canvas_dim": 16384, "max_canvas_pixels": 67108864},
              "config_errors": ["allowed root '~/gone' skipped: does not exist",
                                "config file x: unknown key 'colour'\nignored"]}
    monkeypatch.setattr(client, "get_health", lambda timeout=None: dict(health))
    result = server.krita_health()
    assert result["allowed_dirs"] == ["/out", "/art"]
    assert result["config_errors"] == health["config_errors"]
    assert result["limits"] == health["limits"]
    summary = result["summary"]
    assert "\n" not in summary
    assert "2 allowed dirs" in summary and "2 config errors: " in summary
    assert "'~/gone' skipped: does not exist" in summary and "'colour' ignored" in summary


def test_health_without_config_fields(monkeypatch):
    health = {"status": "ok", "plugin": "kritamcp", "protocol": 2, "version": "0.2.0",
              "krita_version": "5.3.4", "allowed_dirs": ["/out"], "config_errors": []}
    monkeypatch.setattr(client, "get_health", lambda timeout=None: dict(health))
    result = server.krita_health()
    assert result["config_errors"] == [] and "config error" not in result["summary"]
    assert "1 allowed dir" in result["summary"] and "dirs" not in result["summary"]
