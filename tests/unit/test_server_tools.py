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
    "krita_list_documents",
    "krita_get_document_info",
    "krita_close_document",
    "krita_list_layers",
    "krita_create_layers",
    "krita_update_layers",
    "krita_move_layers",
    "krita_delete_layers",
    "krita_import_png",
    "krita_export_layers",
}

# tool -> (kwargs, plugin action, canned plugin payload)
CASES = {
    "krita_new_canvas": ({"width": 64, "height": 48}, "new_canvas",
                         {"width": 64, "height": 48, "name": "New Canvas",
                          "color_profile": "sRGB-elle-V2-srgbtrc.icc", "resolution": 120,
                          "background": "#1a1a2e", "background_layer": "{0000}"}),
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
    "krita_save": ({"path": "a.png"}, "save",
                   {"path": "/out/a.png", "format": "png", "bytes": 2048,
                    "doc_path_changed": False}),
    "krita_get_color_at": ({"x": 1, "y": 1}, "get_color_at",
                           {"color": "#010203", "r": 1, "g": 2, "b": 3, "a": 255}),
    "krita_list_brushes": ({}, "list_brushes", {"brushes": ["b) Basic-1"], "count": 1}),
    "krita_open_file": ({"path": "a.kra"}, "open_file",
                        {"path": "/out/a.kra", "name": "a", "width": 4, "height": 4,
                         "layer_counts": {"paintlayer": 1}, "already_open": False}),
    "krita_list_documents": ({}, "list_documents",
                             {"documents": [{"doc": "doc-1", "name": "a", "path": None,
                                             "width": 4, "height": 4, "modified": False,
                                             "active": True}]}),
    "krita_get_document_info": ({}, "get_document_info",
                                {"name": "a", "path": None, "width": 4, "height": 4,
                                 "resolution": 72, "color_model": "RGBA", "color_depth": "U8",
                                 "color_profile": "sRGB-elle-V2-srgbtrc.icc",
                                 "modified": True, "layer_counts": {"paintlayer": 2},
                                 "max_depth": 1, "selection": None}),
    "krita_close_document": ({"doc": "doc-1"}, "close_document",
                             {"doc": "doc-1", "closed": True}),
    "krita_create_layers": ({"specs": [{"name": "a"}]}, "create_layers",
                            {"doc": "doc-1", "dry_run": False,
                             "created": [{"key": None, "id": "{1}", "name": "a",
                                          "type": "paintlayer", "path": "a"}],
                             "counts": {"paintlayer": 1, "total": 1}}),
    "krita_update_layers": ({"changes": [{"id": "a", "visible": False}]}, "update_layers",
                            {"doc": "doc-1", "dry_run": False, "warnings": [],
                             "updated": [{"id": "{1}", "path": "a", "changed": ["visible"]}]}),
    "krita_move_layers": ({"moves": [{"id": "a", "index": 0}]}, "move_layers",
                          {"doc": "doc-1", "dry_run": False,
                           "moved": [{"id": "{1}", "path": "a", "parent": "/", "index": 0}]}),
    "krita_delete_layers": ({"ids": ["a"]}, "delete_layers",
                            {"doc": "doc-1", "dry_run": False, "deleted": ["{1}"], "count": 1,
                             "warnings": []}),
    "krita_import_png": ({"id": "P", "png": "a.png"}, "import_png",
                         {"doc": "doc-1", "dry_run": False, "layer": "{1}", "png": "/out/a.png",
                          "mode": "replace", "clear_outside": False,
                          "changed": {"x": 0, "y": 0, "w": 4, "h": 3},
                          "bounds": {"x": 0, "y": 0, "w": 4, "h": 3}, "warnings": []}),
    "krita_export_layers": ({"dir": "out"}, "export_layers",
                            {"doc": "doc-1", "dir": "/out/out", "manifest": "/out/out/manifest.json",
                             "count": 1, "layers": [{"key": "L0001", "type": "paint",
                                                     "png": "0001_a.png"}],
                             "skipped": [], "warnings": [], "bytes": 300}),
    "krita_list_layers": ({}, "list_layers",
                          {"root": "/", "layers": [{"id": "{1}", "name": "a", "children": []}],
                           "counts": {"paintlayer": 1, "total": 1}}),
}


class FakeCall:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.calls = []

    def __call__(self, action, params=None, timeout_s=None):
        self.calls.append((action, params, timeout_s))
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


def test_tools_default_to_the_commands_timeout(fake_call):
    server.krita_get_canvas()
    server.krita_save(path="a.png")
    assert [c[2] for c in fake_call.calls] == [None, None]


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_tool_passes_timeout_s(fake_call, name):
    kwargs, action, payload = CASES[name]
    fake_call.payload = payload
    getattr(server, name)(**kwargs, timeout_s=42.5)
    assert fake_call.calls[0][0] == action and fake_call.calls[0][2] == 42.5
    assert "timeout_s" not in (fake_call.calls[0][1] or {})  # client.call adds it


def test_timeout_s_in_every_tool_schema_but_health():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name, tool in tools.items():
        props = tool.parameters.get("properties", {})
        if name == "krita_health":
            assert "timeout_s" not in props
        else:
            assert "timeout_s" in props, name
            assert "timeout_s" not in tool.parameters.get("required", []), name
            assert "up to 900" in props["timeout_s"].get("description", ""), name


def test_set_brush_sends_size_zero(fake_call):
    server.krita_set_brush(size=0)
    assert fake_call.calls == [("set_brush", {"size": 0}, None)]


def test_set_brush_omits_unset_fields(fake_call):
    server.krita_set_brush(opacity=0.0)
    assert fake_call.calls == [("set_brush", {"opacity": 0.0}, None)]


def test_stroke_with_one_point_raises_bad_request(fake_call):
    with pytest.raises(ToolError) as exc:
        server.krita_stroke(points=[[1, 1]])
    assert str(exc.value).startswith("bad_request: ")
    assert fake_call.calls == []


def test_tool_error_propagates(monkeypatch):
    def failing(action, params=None, timeout_s=None):
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


def test_health_reports_busy(monkeypatch):
    health = {"status": "ok", "plugin": "kritamcp", "protocol": 2, "version": "0.2.0",
              "krita_version": "5.3.4", "allowed_dirs": ["/out"], "config_errors": [],
              "busy": {"action": "save", "elapsed_ms": 12345}, "queued": 2}
    monkeypatch.setattr(client, "get_health", lambda timeout=None: dict(health))
    result = server.krita_health()
    assert result["busy"] == {"action": "save", "elapsed_ms": 12345}
    assert result["queued"] == 2
    assert "busy: save for 12.3 s, 2 queued" in result["summary"]


def test_health_idle_has_busy_null(monkeypatch):
    health = {"status": "ok", "plugin": "kritamcp", "protocol": 2, "version": "0.2.0",
              "krita_version": "5.3.4", "busy": None, "queued": 0}
    monkeypatch.setattr(client, "get_health", lambda timeout=None: dict(health))
    result = server.krita_health()
    assert result["busy"] is None and "busy" not in result["summary"]


# P1.6: tools that act on a document take `doc`; painting tools and
# get_color_at also take `layer`. App/UI-state tools take neither.
DOC_TOOLS = {"krita_save", "krita_get_canvas", "krita_stroke", "krita_fill",
             "krita_draw_shape", "krita_clear", "krita_get_color_at",
             "krita_get_document_info", "krita_close_document", "krita_list_layers",
             "krita_create_layers", "krita_update_layers", "krita_move_layers",
             "krita_delete_layers", "krita_import_png", "krita_export_layers"}
# close_document has no active-document default.
REQUIRED_DOC_TOOLS = {"krita_close_document"}
OPTIONAL_DOC_TOOLS = DOC_TOOLS - REQUIRED_DOC_TOOLS
LAYER_TOOLS = {"krita_stroke", "krita_fill", "krita_draw_shape", "krita_clear",
               "krita_get_color_at"}


def test_doc_and_layer_in_schemas():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name, tool in tools.items():
        props = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        assert ("doc" in props) == (name in DOC_TOOLS), name
        assert ("layer" in props) == (name in LAYER_TOOLS), name
        for key in ("doc", "layer"):
            if key in props and not (key == "doc" and name in REQUIRED_DOC_TOOLS):
                assert key not in required, name
                assert props[key].get("default") is None, name
        if name in REQUIRED_DOC_TOOLS:
            assert "doc" in required, name
        if "doc" in props:
            assert "doc-3" in props["doc"]["description"], name
            assert "absolute path" in props["doc"]["description"], name
        if "layer" in props:
            assert "UUID" in props["layer"]["description"], name
            assert "unique name" in props["layer"]["description"], name


@pytest.mark.parametrize("name", sorted(OPTIONAL_DOC_TOOLS))
def test_doc_and_layer_not_sent_when_unset(fake_call, name):
    kwargs, action, payload = CASES[name]
    getattr(server, name)(**kwargs)
    params = fake_call.calls[0][1]
    assert "doc" not in params and "layer" not in params


@pytest.mark.parametrize("name", sorted(DOC_TOOLS))
def test_doc_and_layer_forwarded(fake_call, name):
    kwargs, action, payload = CASES[name]
    kwargs = {k: v for k, v in kwargs.items() if k != "doc"}
    extra = {"doc": "doc-2"}
    if name in LAYER_TOOLS:
        extra["layer"] = "Head/Eyes"
    getattr(server, name)(**kwargs, **extra)
    action_sent, params, _ = fake_call.calls[0]
    assert action_sent == action
    for key, value in extra.items():
        assert params[key] == value
    # The tool's own params are still there.
    for key, value in kwargs.items():
        if key in params:
            assert params[key] == value


@pytest.mark.parametrize("name", sorted(DOC_TOOLS | {"krita_new_canvas", "krita_open_file"}))
def test_summary_mentions_doc_handle(fake_call, name):
    kwargs, action, payload = CASES[name]
    fake_call.payload = {**payload, "doc": "doc-7"}
    result = getattr(server, name)(**kwargs)
    assert "doc-7" in result["summary"], result["summary"]
    assert result["doc"] == "doc-7"


def test_summary_without_doc_has_no_placeholder(fake_call):
    fake_call.payload = {"x": 1, "y": 2, "radius": 3}
    result = server.krita_fill(x=1, y=2, radius=3)
    assert result["summary"] == "Filled circle at (1, 2) with radius 3"


def test_clear_transparent_and_set_color_docstrings():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert '"transparent"' in tools["krita_clear"].parameters["properties"]["color"]["description"]
    assert "without a Krita window" in tools["krita_set_color"].description


def test_set_color_summary_notes_no_view(fake_call):
    fake_call.payload = {"color": "#ff0000", "view_updated": False}
    assert server.krita_set_color("#ff0000")["summary"] == \
        "Color set to #ff0000 (stored; no Krita window to update)"
    fake_call.payload = {"color": "#ff0000", "view_updated": True}
    assert server.krita_set_color("#ff0000")["summary"] == "Color set to #ff0000"


# P1.7: document tools.

def test_new_canvas_sends_profile_and_resolution_only_when_set(fake_call):
    server.krita_new_canvas(width=10, height=10)
    params = fake_call.calls[0][1]
    assert params == {"width": 10, "height": 10, "name": "New Canvas", "background": "#1a1a2e"}
    server.krita_new_canvas(width=10, height=10, background="transparent",
                            color_profile="sRGB built-in", resolution=300.0)
    params = fake_call.calls[1][1]
    assert params["color_profile"] == "sRGB built-in" and params["resolution"] == 300.0
    assert params["background"] == "transparent"


def test_new_canvas_summary(fake_call):
    fake_call.payload = {"doc": "doc-4", "name": "A", "width": 5800, "height": 8400,
                         "color_profile": "sRGB-elle-V2-srgbtrc.icc", "resolution": 120,
                         "background": "transparent", "background_layer": "{1}"}
    summary = server.krita_new_canvas(width=5800, height=8400, name="A",
                                      background="transparent")["summary"]
    assert summary == ("Created canvas 'A' (doc-4): 5800x8400, background transparent, "
                       "sRGB-elle-V2-srgbtrc.icc, 120 dpi")


def test_save_params(fake_call):
    server.krita_save()
    server.krita_save(path="a.kra", doc="doc-2", format="kra")
    assert fake_call.calls[0][1] == {}
    assert fake_call.calls[1][1] == {"path": "a.kra", "doc": "doc-2", "format": "kra"}


def test_save_summaries(fake_call):
    fake_call.payload = {"doc": "doc-2", "path": "/out/a.kra", "format": "kra", "bytes": 20480,
                         "doc_path_changed": True}
    assert server.krita_save(path="a.kra")["summary"] == \
        "Saved doc-2 to /out/a.kra (kra, 20.0 KB); the document now lives there"
    fake_call.payload = {**fake_call.payload, "doc_path_changed": False}
    assert server.krita_save()["summary"] == "Saved doc-2 in place to /out/a.kra (kra, 20.0 KB)"
    fake_call.payload = {"doc": "doc-2", "path": "/out/a.png", "format": "png", "bytes": 100,
                         "doc_path_changed": False}
    assert server.krita_save(path="a.png")["summary"] == \
        "Exported doc-2 to /out/a.png (png, 100 bytes)"


def test_save_summary_mentions_blend_conversions(fake_call):
    add = {"from": "add", "to": "linear_dodge"}
    fake_call.payload = {"doc": "doc-2", "path": "/out/a.psd", "format": "psd", "bytes": 100,
                         "doc_path_changed": False, "blend_conversions": []}
    assert server.krita_save(path="a.psd")["summary"] == \
        "Exported doc-2 to /out/a.psd (psd, 100 bytes)"
    fake_call.payload["blend_conversions"] = [{"id": "{a}", "path": "A", **add},
                                              {"id": "{b}", "path": "G/B", **add}]
    result = server.krita_save(path="a.psd")
    assert result["summary"] == ("Exported doc-2 to /out/a.psd (psd, 100 bytes); "
                                 "2 Add layers written as Linear Dodge for PSD")
    assert result["blend_conversions"] == fake_call.payload["blend_conversions"]
    fake_call.payload = {"doc": "doc-2", "path": "/out/a.ora", "format": "ora", "bytes": 100,
                         "doc_path_changed": False,
                         "blend_conversions": [{"id": "{a}", "path": "A", "from": "linear_dodge",
                                                "to": "add"}]}
    assert server.krita_save()["summary"] == ("Saved doc-2 in place to /out/a.ora (ora, 100 bytes); "
                                              "1 Linear Dodge layer written as Add for ORA")


def test_save_schema_path_optional():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    params = tools["krita_save"].parameters
    assert "path" not in params.get("required", [])
    assert set(params["properties"]) >= {"path", "doc", "format", "timeout_s"}


def test_open_file_already_open_summary(fake_call):
    fake_call.payload = {"doc": "doc-1", "path": "/out/a.kra", "name": "a", "width": 4,
                         "height": 4, "layer_counts": {"paintlayer": 2, "grouplayer": 1},
                         "already_open": True}
    assert server.krita_open_file(path="a.kra")["summary"] == \
        "a is already open as doc-1 (4x4, 3 layers)"
    fake_call.payload = {**fake_call.payload, "already_open": False}
    assert server.krita_open_file(path="a.kra")["summary"] == "Opened a as doc-1 (4x4, 3 layers)"


def test_list_documents_summary(fake_call):
    fake_call.payload = {"documents": []}
    assert server.krita_list_documents()["summary"] == "0 open documents"
    fake_call.payload = {"documents": [
        {"doc": "doc-1", "name": "A", "active": False},
        {"doc": "doc-2", "name": "B", "active": True}]}
    assert server.krita_list_documents()["summary"] == \
        "2 open documents: doc-1 'A', doc-2 'B' (active)"
    fake_call.payload = {"documents": [{"doc": f"doc-{i}", "name": "x", "active": False}
                                       for i in range(12)]}
    summary = server.krita_list_documents()["summary"]
    assert summary.startswith("12 open documents: ") and summary.endswith(", and 2 more")


def test_get_document_info_summary(fake_call):
    fake_call.payload = {**CASES["krita_get_document_info"][2], "doc": "doc-1",
                         "selection": {"x": 0, "y": 0, "w": 2, "h": 2}}
    assert server.krita_get_document_info(doc="doc-1")["summary"] == (
        "doc-1 'a': 4x4, RGBA/U8 sRGB-elle-V2-srgbtrc.icc, 2 layers, depth 1, modified, "
        "has a selection")


def test_close_document_params_and_summary(fake_call):
    fake_call.payload = {"doc": "doc-1", "closed": True}
    assert server.krita_close_document("doc-1")["summary"] == "Closed doc-1"
    assert fake_call.calls[0][1] == {"doc": "doc-1", "discard": False}
    result = server.krita_close_document("doc-1", discard=True)
    assert fake_call.calls[1][1] == {"doc": "doc-1", "discard": True}
    assert "discarded" in result["summary"]


# P2.1: list_layers.

def test_list_layers_forwards_only_set_params(fake_call):
    server.krita_list_layers()
    assert fake_call.calls[0][1] == {}
    server.krita_list_layers(doc="doc-2", root="Head", depth=2, fields=["bounds"])
    assert fake_call.calls[1][1] == {"doc": "doc-2", "root": "Head", "depth": 2,
                                     "fields": ["bounds"]}
    server.krita_list_layers(fields=[])  # an empty list is sent: only id, name, children
    assert fake_call.calls[2][1] == {"fields": []}


def test_list_layers_schema():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools["krita_list_layers"]
    props = tool.parameters["properties"]
    assert set(props) == {"doc", "root", "depth", "fields", "timeout_s"}
    assert not tool.parameters.get("required")
    assert "top to bottom" in tool.description and "not clipped" in tool.description


def test_list_layers_summary(fake_call):
    fake_call.payload = {"doc": "doc-2", "root": "/", "layers": [{"id": "{1}"}],
                         "counts": {"paintlayer": 184, "grouplayer": 38, "total": 222}}
    assert server.krita_list_layers()["summary"] == "38 groups, 184 paint layers in doc-2"
    fake_call.payload = {"doc": "doc-2", "root": "{1}",
                         "layers": [{"id": "{1}", "name": "Eyes", "path": "Head/Eyes"}],
                         "counts": {"grouplayer": 1, "paintlayer": 1, "transparencymask": 2,
                                    "total": 4}}
    assert server.krita_list_layers(root="{1}", depth=1)["summary"] == (
        "1 group, 1 paint layer, 2 transparency masks under 'Head/Eyes' in doc-2 (depth 1)")
    fake_call.payload = {"doc": "doc-2", "root": "/", "layers": [], "counts": {"total": 0}}
    assert server.krita_list_layers()["summary"] == "No layers in doc-2"
    fake_call.payload = {"root": "/", "layers": [],
                         "counts": {"weirdlayer": 2, "total": 2}}
    assert server.krita_list_layers()["summary"] == "2 weirdlayer in the active document"


# P2.3: create_layers.

def test_create_layers_sends_only_set_fields(fake_call):
    from krita_mcp_server.models import LayerSpec
    server.krita_create_layers([{"name": "G", "type": "group", "key": "g"},
                                LayerSpec(name="A", parent="$g", visible=False, opacity=0.0,
                                          x=0, y=0, png="a.png")])
    action, params, timeout_s = fake_call.calls[0]
    assert action == "create_layers" and timeout_s is None
    assert params == {"specs": [{"name": "G", "type": "group", "key": "g"},
                                {"name": "A", "parent": "$g", "visible": False, "opacity": 0.0,
                                 "png": "a.png", "x": 0, "y": 0}]}
    server.krita_create_layers([{"name": "A"}], doc="doc-2", dry_run=True, timeout_s=60)
    assert fake_call.calls[1][1:] == ({"specs": [{"name": "A"}], "doc": "doc-2",
                                       "dry_run": True}, 60)


def test_create_layers_schema_and_typos():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools["krita_create_layers"]
    assert set(tool.parameters["properties"]) == {"specs", "manifest", "doc", "dry_run",
                                                  "timeout_s"}
    assert not tool.parameters.get("required")
    assert "bottom to top" in tool.description and "$<key>" in tool.description
    assert "relative to the manifest's directory" in " ".join(tool.description.split())

    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool("krita_create_layers",
                                     {"specs": [{"name": "A", "opactiy": 0.5}]},
                                     raise_on_error=False)
    result = asyncio.run(go())
    assert result.is_error


def test_create_layers_summary(fake_call):
    fake_call.payload = {"doc": "doc-2", "dry_run": False, "created": [{}, {}, {}],
                         "counts": {"grouplayer": 1, "paintlayer": 2, "total": 3}}
    assert server.krita_create_layers([{"name": "a"}])["summary"] == (
        "Created 3 layers (1 group) in doc-2")
    fake_call.payload = {"doc": "doc-2", "dry_run": True, "would_create": [{}],
                         "counts": {"paintlayer": 1, "total": 1}}
    assert server.krita_create_layers([{"name": "a"}], dry_run=True)["summary"] == (
        "Would create 1 layer in doc-2 (dry run, nothing changed)")



# P2.4: create_layers(manifest=…).

def test_create_layers_manifest_forwarded(fake_call):
    fake_call.payload = {"doc": "doc-2", "dry_run": False, "created": [{}, {}],
                         "counts": {"grouplayer": 1, "paintlayer": 1, "total": 2},
                         "manifest": "/out/m/manifest.json", "skipped": [], "warnings": []}
    result = server.krita_create_layers(manifest="m/manifest.json", doc="doc-2", timeout_s=600)
    assert fake_call.calls == [("create_layers", {"manifest": "m/manifest.json",
                                                  "doc": "doc-2"}, 600)]
    assert result["summary"] == "Created 2 layers (1 group) in doc-2 from /out/m/manifest.json"
    server.krita_create_layers(manifest="m.json", dry_run=True)
    assert fake_call.calls[1][1] == {"manifest": "m.json", "dry_run": True}


def test_create_layers_needs_exactly_one_input(fake_call):
    for kwargs in ({}, {"specs": [{"name": "a"}], "manifest": "m.json"}):
        with pytest.raises(ToolError) as e:
            server.krita_create_layers(**kwargs)
        assert str(e.value).startswith("bad_request: ") and "exactly one" in str(e.value)
    assert fake_call.calls == []


def test_create_layers_manifest_summary_counts_skipped_and_warnings(fake_call):
    fake_call.payload = {"doc": "doc-2", "dry_run": True, "would_create": [{}],
                         "counts": {"paintlayer": 1, "total": 1},
                         "manifest": "/out/manifest.json",
                         "skipped": [{"index": 0, "key": "P", "reason": "group projection"}] * 2,
                         "warnings": [{"code": "document_size",
                                       "message": "The manifest was made for\na 8x8 document"}]}
    assert server.krita_create_layers(manifest="manifest.json", dry_run=True)["summary"] == (
        "Would create 1 layer in doc-2 from /out/manifest.json (2 entries skipped, "
        "1 warning: The manifest was made for a 8x8 document) (dry run, nothing changed)")


# P2.5: update_layers, move_layers, delete_layers.

def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def _call_tool(name, args):
    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)
    return asyncio.run(go())


def test_update_layers_sends_only_set_fields(fake_call):
    from krita_mcp_server.models import LayerChange
    server.krita_update_layers([{"id": "A", "visible": False, "opacity": 0},
                                LayerChange(id="G", pass_through=False, locked=False)])
    action, params, timeout_s = fake_call.calls[0]
    assert (action, timeout_s) == ("update_layers", None)
    assert params == {"changes": [{"id": "A", "visible": False, "opacity": 0.0},
                                  {"id": "G", "locked": False, "pass_through": False}]}
    server.krita_update_layers([{"id": "A", "name": "B"}], doc="doc-2", dry_run=True, timeout_s=9)
    assert fake_call.calls[1][1:] == ({"changes": [{"id": "A", "name": "B"}], "doc": "doc-2",
                                       "dry_run": True}, 9)


def test_move_layers_sends_only_set_fields(fake_call):
    server.krita_move_layers([{"id": "A", "index": 0}, {"id": "B", "parent": "/"},
                              {"id": "C", "below": "B"}])
    assert fake_call.calls[0][1] == {"moves": [{"id": "A", "index": 0},
                                               {"id": "B", "parent": "/"},
                                               {"id": "C", "below": "B"}]}
    server.krita_move_layers([{"id": "A"}], dry_run=True)
    assert fake_call.calls[1][1] == {"moves": [{"id": "A"}], "dry_run": True}


def test_delete_layers_params(fake_call):
    server.krita_delete_layers(["A", "G/H"])
    assert fake_call.calls[0] == ("delete_layers", {"ids": ["A", "G/H"]}, None)
    server.krita_delete_layers(["A"], doc="doc-3", dry_run=True)
    assert fake_call.calls[1][1] == {"ids": ["A"], "doc": "doc-3", "dry_run": True}


def test_update_move_delete_schemas_and_typos():
    tools = _tools()
    for name, items in (("krita_update_layers", "changes"), ("krita_move_layers", "moves"),
                        ("krita_delete_layers", "ids")):
        params = tools[name].parameters
        assert set(params["properties"]) == {items, "doc", "dry_run", "timeout_s"}, name
        assert params["required"] == [items], name
    move_doc = " ".join(tools["krita_move_layers"].description.split())
    assert "from the BOTTOM" in move_doc and "on top" in move_doc and "own subtree" in move_doc
    assert "whole subtree" in tools["krita_delete_layers"].description
    assert "0.0-1.0" in tools["krita_update_layers"].description
    for name, args in (("krita_update_layers", {"changes": [{"id": "A", "opactiy": 0.5}]}),
                       ("krita_move_layers", {"moves": [{"id": "A", "abve": "B"}]}),
                       ("krita_move_layers", {"moves": [{"id": "A", "above": "B", "index": 0}]}),
                       ("krita_delete_layers", {"ids": "A"})):
        assert _call_tool(name, args).is_error, (name, args)


def test_update_layers_summary(fake_call):
    fake_call.payload = {"doc": "doc-2", "dry_run": False, "updated": [{}, {}],
                         "warnings": [{"code": "duplicate_name", "message": "Layer {1} is\nnow"}]}
    assert server.krita_update_layers([{"id": "a", "name": "b"}])["summary"] == (
        "Updated 2 layers in doc-2 (1 warning: Layer {1} is now)")
    fake_call.payload = {"doc": "doc-2", "dry_run": True, "would_update": [{}], "warnings": []}
    assert server.krita_update_layers([{"id": "a", "visible": True}], dry_run=True)["summary"] == (
        "Would update 1 layer in doc-2 (dry run, nothing changed)")


def test_move_layers_summary(fake_call):
    fake_call.payload = {"doc": "doc-2", "dry_run": False, "moved": [{}, {}, {}]}
    assert server.krita_move_layers([{"id": "a"}])["summary"] == "Moved 3 layers in doc-2"
    fake_call.payload = {"doc": "doc-2", "dry_run": True, "would_move": [{}]}
    assert server.krita_move_layers([{"id": "a"}], dry_run=True)["summary"] == (
        "Would move 1 layer in doc-2 (dry run, nothing changed)")


def test_delete_layers_summary(fake_call):
    fake_call.payload = {"doc": "doc-2", "dry_run": False, "deleted": ["{1}", "{2}", "{3}"],
                         "count": 3, "warnings": []}
    assert server.krita_delete_layers(["G"])["summary"] == (
        "Deleted 3 layers (subtrees included) in doc-2")
    assert server.krita_delete_layers(["A", "B", "C"])["summary"] == "Deleted 3 layers in doc-2"
    fake_call.payload = {"doc": "doc-2", "dry_run": True, "would_delete": ["{1}"], "count": 1}
    assert server.krita_delete_layers(["A"], dry_run=True)["summary"] == (
        "Would delete 1 layer in doc-2 (dry run, nothing changed)")
    fake_call.payload = {"doc": "doc-2", "dry_run": False, "deleted": ["{1}"], "count": 1,
                         "warnings": [{"code": "not_undoable", "message": "too big"}]}
    assert server.krita_delete_layers(["A"])["summary"] == (
        "Deleted 1 layer in doc-2 (1 warning: too big)")


# P3.3: import_png.

def test_import_png_params(fake_call):
    server.krita_import_png("P", "a.png")
    assert fake_call.calls[0] == ("import_png", {"id": "P", "png": "a.png", "x": 0, "y": 0,
                                                 "mode": "replace"}, None)
    server.krita_import_png("G/P", "~/b.png", x=-3, y=7, mode="over", doc="doc-2",
                            dry_run=True, timeout_s=60)
    assert fake_call.calls[1] == ("import_png", {"id": "G/P", "png": "~/b.png", "x": -3, "y": 7,
                                                 "mode": "over", "doc": "doc-2",
                                                 "dry_run": True}, 60)
    server.krita_import_png("P", "a.png", clear_outside=True)
    assert fake_call.calls[2][1]["clear_outside"] is True


def test_import_png_schema_and_typos():
    params = _tools()["krita_import_png"].parameters
    assert set(params["properties"]) == {"id", "png", "x", "y", "mode", "clear_outside", "doc",
                                         "dry_run", "timeout_s"}
    assert sorted(params["required"]) == ["id", "png"]
    assert params["properties"]["mode"]["enum"] == ["replace", "over"]
    description = " ".join(_tools()["krita_import_png"].description.split())
    assert "outside it are untouched unless clear_outside" in description
    assert "8-bit RGBA" in description and "paint layer" in description
    for args in ({"id": "P", "png": "a.png", "mode": "under"},
                 {"id": "P", "png": "a.png", "x": 1.5},
                 {"id": "P"}):
        assert _call_tool("krita_import_png", args).is_error, args


def test_import_png_summary(fake_call):
    fake_call.payload = {"doc": "doc-2", "dry_run": False, "layer": "{1}", "png": "/out/a.png",
                         "mode": "replace", "clear_outside": True,
                         "changed": {"x": 2, "y": 3, "w": 40, "h": 30}, "bounds": None,
                         "warnings": [{"code": "not_undoable", "message": "too big"}]}
    assert server.krita_import_png("P", "a.png", clear_outside=True)["summary"] == (
        "Imported a.png into layer {1} (replace, clear outside), changing 40x30 at (2, 3) "
        "in doc-2 (1 warning: too big)")
    fake_call.payload = {"doc": "doc-2", "dry_run": True, "layer": "{1}", "png": "/out/a.png",
                         "mode": "over", "clear_outside": False,
                         "would_change": {"x": 0, "y": 0, "w": 4, "h": 4}, "warnings": []}
    assert server.krita_import_png("P", "a.png", mode="over", dry_run=True)["summary"] == (
        "Would import a.png into layer {1} (over), changing 4x4 at (0, 0) in doc-2 "
        "(dry run, nothing changed)")


# P3.2: export_layers.

def test_export_layers_params(fake_call):
    server.krita_export_layers("out")
    assert fake_call.calls[0] == ("export_layers", {"dir": "out"}, None)
    server.krita_export_layers("~/x", ids=["Head", "{1}"], crop=False, include_hidden=False,
                               include_groups=True, overwrite=True, png_level=1, doc="doc-2",
                               timeout_s=600)
    assert fake_call.calls[1] == ("export_layers", {
        "dir": "~/x", "ids": ["Head", "{1}"], "crop": False, "include_hidden": False,
        "include_groups": True, "overwrite": True, "png_level": 1, "doc": "doc-2"}, 600)
    server.krita_export_layers("out", ids="all", png_level=0)
    assert fake_call.calls[2][1] == {"dir": "out", "png_level": 0}


def test_export_layers_validates_ids_and_png_level(fake_call):
    for bad in (10, -1, True):
        with pytest.raises(ToolError, match="^bad_request: png_level"):
            server.krita_export_layers("out", png_level=bad)
    with pytest.raises(ToolError, match="^bad_request: ids"):
        server.krita_export_layers("out", ids="Head")
    assert fake_call.calls == []


def test_export_layers_schema_and_typos():
    params = _tools()["krita_export_layers"].parameters
    assert set(params["properties"]) == {"dir", "ids", "crop", "include_hidden", "include_groups",
                                         "overwrite", "png_level", "doc", "timeout_s"}
    assert params["required"] == ["dir"]
    ids = params["properties"]["ids"]
    assert ids["default"] == "all"
    assert {"const": "all", "type": "string"} in ids["anyOf"]
    level = params["properties"]["png_level"]["anyOf"][0]
    assert (level["minimum"], level["maximum"]) == (0, 9)
    description = " ".join(_tools()["krita_export_layers"].description.split())
    for phrase in ("create order", "png null", '"projection": true', "krita_create_layers",
                   "png_level=1", "8-bit RGBA"):
        assert phrase in description, phrase
    for args in ({"dir": "out", "png_level": 10}, {"dir": "out", "ids": "Head"},
                 {"dir": "out", "ids": [1]}, {"ids": "all"}, {"dir": "out", "crop": "no"}):
        assert _call_tool("krita_export_layers", args).is_error, args


def test_export_layers_summary(fake_call):
    fake_call.payload = {
        "doc": "doc-2", "dir": "/out/x", "manifest": "/out/x/manifest.json", "count": 2,
        "layers": [{"type": "group"}, {"type": "paint", "png": "a"},
                   {"type": "paint", "png": None}, {"type": "paint", "projection": True,
                                                    "png": "b"}],
        "skipped": [{"id": "{9}", "path": "F", "type": "filterlayer",
                     "reason": "unsupported_type"}],
        "warnings": [{"code": "not_exported", "message": "1 layer(s) of other types"}],
        "bytes": 4096}
    assert server.krita_export_layers("x")["summary"] == (
        "Exported 2 PNGs for 2 layers and 1 group (1 group projection) in doc-2 to /out/x, "
        "4.0 KB, 1 skipped (1 warning: 1 layer(s) of other types)")
