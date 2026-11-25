"""krita_mcp_server.client with the HTTP calls mocked (no Krita)."""

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from krita_mcp_server import client

HEALTH_OK = {"status": "ok", "plugin": "kritamcp", "protocol": 2,
             "version": "0.2.0", "krita_version": "5.3.4"}


def _response(status, body, url=client.KRITA_URL):
    request = httpx.Request("POST", url)
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, content=body, request=request)


class FakeKrita:
    """Stands in for httpx.get/httpx.post and records the calls."""

    def __init__(self, post=None, health=HEALTH_OK, health_status=200):
        self.post_reply = post if post is not None else _response(
            200, {"ok": True, "result": {}, "elapsed_ms": 1})
        self.health = health
        self.health_status = health_status
        self.gets = []
        self.posts = []

    def get(self, url, timeout=None, **kwargs):
        self.gets.append(url)
        if isinstance(self.health, Exception):
            raise self.health
        return _response(self.health_status, self.health, url)

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if isinstance(self.post_reply, Exception):
            raise self.post_reply
        return self.post_reply


@pytest.fixture(autouse=True)
def fresh_protocol_check():
    client.reset_protocol_check()
    yield
    client.reset_protocol_check()


@pytest.fixture
def krita(monkeypatch):
    fake = FakeKrita()
    monkeypatch.setattr(client.httpx, "get", fake.get)
    monkeypatch.setattr(client.httpx, "post", fake.post)
    monkeypatch.setenv("KRITA_MCP_TOKEN", "tok")
    return fake


def test_ok_envelope_returns_payload_with_elapsed_ms(krita):
    krita.post_reply = _response(200, {"ok": True, "result": {"brushes": ["a"], "count": 1},
                                       "elapsed_ms": 7})
    assert client.call("list_brushes", {"limit": 5}, timeout=12.0) == {
        "brushes": ["a"], "count": 1, "elapsed_ms": 7}
    (post,) = krita.posts
    assert post["json"] == {"action": "list_brushes", "params": {"limit": 5}}
    assert post["headers"] == {client.TOKEN_HEADER: "tok"}
    assert post["timeout"] == 12.0


def test_params_default_to_empty_object(krita):
    client.call("undo")
    assert krita.posts[0]["json"] == {"action": "undo", "params": {}}


@pytest.mark.parametrize("code,status", [
    ("bad_request", 400), ("forbidden_path", 403), ("not_found", 404), ("ambiguous", 409),
    ("conflict", 409), ("too_large", 413), ("krita_error", 500), ("timeout", 504),
])
def test_error_envelope_becomes_tool_error(krita, code, status):
    krita.post_reply = _response(status, {"ok": False, "error": {"code": code, "message": "boom"},
                                          "elapsed_ms": 2})
    with pytest.raises(ToolError) as exc:
        client.call("fill")
    assert str(exc.value) == f"{code}: boom"


def test_error_details_appended_as_json(krita):
    details = {"matches": ["{a}", "{b}"]}
    krita.post_reply = _response(409, {"ok": False, "error": {
        "code": "ambiguous", "message": "two layers named 'Hair'", "details": details}})
    with pytest.raises(ToolError) as exc:
        client.call("fill")
    assert str(exc.value) == "ambiguous: two layers named 'Hair' " + json.dumps(details)


def test_legacy_401_is_unauthorized_and_names_token_file(krita):
    krita.post_reply = _response(401, {"error": "Missing or invalid token"})
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    msg = str(exc.value)
    assert msg.startswith("unauthorized: Missing or invalid token")
    assert client.TOKEN_FILE in msg


@pytest.mark.parametrize("status,code", [
    (400, "bad_request"), (403, "forbidden"), (404, "not_found"),
    (413, "too_large"), (415, "bad_request"), (500, "krita_error"),
])
def test_legacy_errors_get_a_code_from_the_status(krita, status, code):
    krita.post_reply = _response(status, {"error": "nope"})
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    assert str(exc.value) == f"{code}: nope"


def test_connect_error_is_unavailable(krita):
    krita.post_reply = httpx.ConnectError("refused")
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    assert str(exc.value) == client.UNAVAILABLE
    assert str(exc.value).startswith("unavailable: Cannot connect to Krita")


def test_transport_timeout(krita):
    krita.post_reply = httpx.ReadTimeout("slow")
    with pytest.raises(ToolError) as exc:
        client.call("save", timeout=3.0)
    assert str(exc.value).startswith("timeout: ")


def test_non_json_is_krita_error(krita):
    krita.post_reply = _response(200, b"<html>")
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    assert str(exc.value).startswith("krita_error: ")


@pytest.mark.parametrize("health", [
    {"status": "ok", "plugin": "kritamcp"},                 # pre-protocol plugin
    {"status": "ok", "plugin": "kritamcp", "protocol": 1},
    {"status": "ok", "plugin": "kritamcp", "protocol": 3},
])
def test_protocol_mismatch_refuses_to_post(krita, health):
    krita.health = health
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    msg = str(exc.value)
    assert msg.startswith("protocol_mismatch: ")
    assert "restart Krita" in msg
    assert ("newer" if health.get("protocol") == 3 else "older") in msg
    assert krita.posts == []


def test_protocol_checked_once_per_process(krita):
    client.call("undo")
    client.call("redo")
    client.call("undo")
    assert len(krita.gets) == 1
    assert krita.gets[0].endswith("/health")
    assert len(krita.posts) == 3


def test_failed_protocol_check_is_retried(krita):
    krita.health = httpx.ConnectError("refused")
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    assert str(exc.value).startswith("unavailable: ")
    krita.health = HEALTH_OK
    client.call("undo")
    client.call("undo")
    assert len(krita.gets) == 2
    assert len(krita.posts) == 2


def test_mismatch_is_not_cached(krita):
    krita.health = {"status": "ok"}
    with pytest.raises(ToolError):
        client.call("undo")
    krita.health = HEALTH_OK
    client.call("undo")
    assert len(krita.gets) == 2


def test_health_refused_uses_legacy_code(krita):
    krita.health, krita.health_status = {"error": "Forbidden host"}, 403
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    assert str(exc.value) == "forbidden: Forbidden host"


def test_token_file_used_when_env_unset(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    monkeypatch.delenv("KRITA_MCP_TOKEN", raising=False)
    monkeypatch.setattr(client, "TOKEN_FILE", str(token_file))
    assert client.auth_headers() == {client.TOKEN_HEADER: "from-file"}
    monkeypatch.setattr(client, "TOKEN_FILE", str(tmp_path / "missing"))
    assert client.auth_headers() == {client.TOKEN_HEADER: ""}


def test_protocol_matches_plugin():
    from kritamcp import version
    assert client.PROTOCOL == version.PROTOCOL == 2
