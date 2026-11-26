"""krita_mcp_server.client with the HTTP calls mocked (no Krita)."""

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from krita_mcp_server import client

HEALTH_OK = {"status": "ok", "plugin": "kritamcp", "protocol": 3,
             "version": "0.3.0", "krita_version": "5.3.4"}
INFO_OK = {"status": "ok", "protocol": 3, "commands": ["fill", "save", "undo"],
           "timeouts": {"fill": 30, "save": 300, "undo": 30}, "mutating": ["fill"]}


def _response(status, body, url=client.KRITA_URL):
    request = httpx.Request("POST", url)
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, content=body, request=request)


class FakeKrita:
    """Stands in for httpx.get/httpx.post and records the calls."""

    def __init__(self, post=None, health=HEALTH_OK, health_status=200, info=INFO_OK,
                 info_status=200):
        self.post_reply = post if post is not None else _response(
            200, {"ok": True, "result": {}, "elapsed_ms": 1})
        self.health = health
        self.health_status = health_status
        self.info = info
        self.info_status = info_status
        self.gets = []
        self.info_headers = []
        self.posts = []

    def get(self, url, timeout=None, headers=None, **kwargs):
        self.gets.append(url)
        if url.endswith("/info"):
            self.info_headers.append(headers)
            body, status = self.info, self.info_status
        else:
            body, status = self.health, self.health_status
        if isinstance(body, Exception):
            raise body
        return _response(status, body, url)

    def health_gets(self):
        return [u for u in self.gets if u.endswith("/health")]

    def info_gets(self):
        return [u for u in self.gets if u.endswith("/info")]

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
    assert client.call("list_brushes", {"limit": 5}) == {
        "brushes": ["a"], "count": 1, "elapsed_ms": 7}
    (post,) = krita.posts
    assert post["json"] == {"action": "list_brushes", "params": {"limit": 5}}
    assert post["headers"] == {client.TOKEN_HEADER: "tok"}


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
        client.call("save", timeout_s=3.0)
    assert str(exc.value).startswith("timeout: ")
    assert "18 s" in str(exc.value)  # 3 + 15


def test_non_json_is_krita_error(krita):
    krita.post_reply = _response(200, b"<html>")
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    assert str(exc.value).startswith("krita_error: ")


@pytest.mark.parametrize("health", [
    {"status": "ok", "plugin": "kritamcp"},                 # pre-protocol plugin
    {"status": "ok", "plugin": "kritamcp", "protocol": 2},  # F001's plugin
    {"status": "ok", "plugin": "kritamcp", "protocol": 4},
])
def test_protocol_mismatch_refuses_to_post(krita, health):
    krita.health = health
    with pytest.raises(ToolError) as exc:
        client.call("undo")
    msg = str(exc.value)
    assert msg.startswith("protocol_mismatch: ")
    assert "restart Krita" in msg
    assert ("newer" if health.get("protocol") == 4 else "older") in msg
    assert krita.posts == []


def test_protocol_checked_once_per_process(krita):
    client.call("undo")
    client.call("redo")
    client.call("undo")
    assert len(krita.health_gets()) == 1
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
    assert len(krita.health_gets()) == 2
    assert len(krita.posts) == 2


def test_mismatch_is_not_cached(krita):
    krita.health = {"status": "ok"}
    with pytest.raises(ToolError):
        client.call("undo")
    krita.health = HEALTH_OK
    client.call("undo")
    assert len(krita.health_gets()) == 2


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
    assert client.PROTOCOL == version.PROTOCOL == 3


# --- timeouts (D9) ---

def test_transport_timeout_is_default_plus_margin(krita):
    client.call("undo")
    client.call("save", {"path": "a.png"})
    assert [p["timeout"] for p in krita.posts] == [30 + 15, 300 + 15]
    assert krita.posts[1]["json"] == {"action": "save", "params": {"path": "a.png"}}


def test_info_fetched_once_with_token_after_protocol_check(krita):
    for _ in range(3):
        client.call("fill")
    assert krita.gets[0].endswith("/health") and krita.gets[1].endswith("/info")
    assert len(krita.info_gets()) == 1
    assert krita.info_headers == [{client.TOKEN_HEADER: "tok"}]


def test_timeout_s_overrides_and_is_sent_in_params(krita):
    client.call("save", {"path": "a.png"}, timeout_s=600)
    (post,) = krita.posts
    assert post["timeout"] == 600 + 15
    assert post["json"] == {"action": "save", "params": {"path": "a.png", "timeout_s": 600}}


def test_timeout_s_does_not_modify_callers_params(krita):
    params = {"x": 1}
    client.call("fill", params, timeout_s=2)
    assert params == {"x": 1}


def test_out_of_range_timeout_s_is_sent_with_a_bounded_transport_timeout(krita):
    # The plugin refuses these with bad_request; the transport stays sane.
    client.call("fill", timeout_s=5000)
    client.call("fill", timeout_s=-1)
    assert [p["timeout"] for p in krita.posts] == [900 + 15, 0 + 15]
    assert [p["json"]["params"]["timeout_s"] for p in krita.posts] == [5000, -1]


def test_unknown_action_uses_fallback(krita):
    client.call("not_listed")
    assert krita.posts[0]["timeout"] == client.FALLBACK_TIMEOUT_S + 15 == 315


@pytest.mark.parametrize("info,status", [
    (httpx.ConnectError("refused"), 200),
    ({"error": "Missing or invalid token"}, 401),
    (b"<html>", 200),
    ({"status": "ok", "protocol": 3, "commands": []}, 200),  # no timeouts
])
def test_info_failure_falls_back_and_is_retried(krita, info, status):
    krita.info, krita.info_status = info, status
    client.call("undo")
    assert krita.posts[0]["timeout"] == client.FALLBACK_TIMEOUT_S + 15
    krita.info, krita.info_status = INFO_OK, 200
    client.call("undo")
    client.call("undo")
    assert [p["timeout"] for p in krita.posts[1:]] == [45, 45]
    assert len(krita.info_gets()) == 2


def test_reset_forgets_timeouts(krita):
    client.call("undo")
    client.reset_protocol_check()
    client.call("undo")
    assert len(krita.info_gets()) == 2
