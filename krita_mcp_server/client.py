"""HTTP client for the Krita plugin (D2, plan.md finding 11).

``call(action, params, timeout)`` posts one command and returns the
plugin's payload plus ``elapsed_ms``, or raises ``ToolError`` with a message
of the form ``"<code>: <message>"`` (details appended as JSON).

Codes are the plugin's (D2: ``bad_request``, ``not_found``, ``conflict``,
``timeout``, ``krita_error``, ...), plus the ones this side adds for
failures that happen before a command runs:

- ``unavailable``: Krita isn't reachable.
- ``unauthorized`` / ``forbidden`` / ``too_large`` / ``bad_request`` /
  ``not_found``: the plugin refused the request (HTTP 401/403/413/415/400/404).
- ``timeout``: no HTTP answer within the transport timeout.
- ``krita_error``: the answer isn't the expected JSON.
- ``protocol_mismatch``: the plugin speaks another protocol version.

Before the first command of the process, ``call`` checks ``/health`` for
``protocol == PROTOCOL``. Only a successful check is remembered.
"""

import json
import os
import threading

import httpx
from fastmcp.exceptions import ToolError

# Must equal the plugin's kritamcp/version.py PROTOCOL.
PROTOCOL = 2

KRITA_URL = os.environ.get("KRITA_URL", "http://localhost:5678")
TOKEN_FILE = os.path.expanduser(os.environ.get("KRITA_MCP_TOKEN_FILE", "~/.krita-mcp-token"))
TOKEN_HEADER = "X-Krita-MCP-Token"

HEALTH_TIMEOUT = 10.0

UNAVAILABLE = "unavailable: Cannot connect to Krita. Is Krita running with the MCP plugin enabled?"

# HTTP status of a refused request (legacy {"error": "<string>"} body) -> code.
_LEGACY_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    413: "too_large",
    415: "bad_request",
    504: "timeout",
}

_protocol_lock = threading.Lock()
_protocol_ok = False


def auth_headers() -> dict:
    """The shared-token header. ``KRITA_MCP_TOKEN`` wins over the token file.

    Read on every call so the server works even if Krita creates the token
    after this process starts.
    """
    token = os.environ.get("KRITA_MCP_TOKEN")
    if not token:
        try:
            with open(TOKEN_FILE) as f:
                token = f.read().strip()
        except OSError:
            token = ""
    return {TOKEN_HEADER: token}


def reset_protocol_check():
    """Forget a successful protocol check (tests; or after Krita restarts)."""
    global _protocol_ok
    with _protocol_lock:
        _protocol_ok = False


def protocol_mismatch_message(reported):
    if reported is None:
        relation = "older than this server (it reports no protocol version)"
    elif isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported > PROTOCOL:
        relation = f"newer than this server (plugin protocol {reported})"
    else:
        relation = f"older than this server (plugin protocol {reported!r})"
    return (f"protocol_mismatch: The Krita plugin is {relation}; this server needs "
            f"protocol {PROTOCOL}. Install the plugin from the same krita-mcp checkout "
            f"as server.py and restart Krita.")


def _url(path=""):
    return KRITA_URL.rstrip("/") + path


def _decode(response):
    try:
        return response.json()
    except ValueError:
        raise ToolError(f"krita_error: Krita sent a response that is not JSON "
                        f"(HTTP {response.status_code})") from None


def _legacy_error(status, message):
    code = _LEGACY_CODES.get(status, "krita_error")
    text = f"{code}: {message}"
    if code == "unauthorized":
        text += (f". The server sends the token from {TOKEN_FILE} (or KRITA_MCP_TOKEN); it "
                 f"must match the one the Krita plugin created in ~/.krita-mcp-token. "
                 f"Check the file exists, or restart Krita to create it.")
    return ToolError(text)


def envelope_error(error):
    """ToolError for an ``{"ok": false, "error": {...}}`` envelope."""
    if not isinstance(error, dict):
        return ToolError(f"krita_error: {error}")
    text = f"{error.get('code', 'krita_error')}: {error.get('message', '')}"
    details = error.get("details")
    if details:
        text += " " + json.dumps(details)
    return ToolError(text)


def unwrap(status, data):
    """Turn a decoded plugin response into a payload dict, or raise ToolError."""
    if not isinstance(data, dict):
        raise ToolError(f"krita_error: unexpected response from Krita (HTTP {status})")
    if data.get("ok") is True:
        result = data.get("result")
        if not isinstance(result, dict):
            raise ToolError("krita_error: Krita's result is not an object")
        out = dict(result)
        if "elapsed_ms" in data:
            out["elapsed_ms"] = data["elapsed_ms"]
        return out
    if data.get("ok") is False:
        raise envelope_error(data.get("error"))
    if isinstance(data.get("error"), str):
        raise _legacy_error(status, data["error"])
    raise ToolError(f"krita_error: unexpected response from Krita (HTTP {status})")


def get_health(timeout=HEALTH_TIMEOUT) -> dict:
    """GET /health and return its JSON, or raise ToolError."""
    try:
        response = httpx.get(_url("/health"), timeout=timeout)
    except httpx.ConnectError:
        raise ToolError(UNAVAILABLE) from None
    except httpx.TimeoutException:
        raise ToolError(f"timeout: Krita did not answer /health within {timeout:g} s") from None
    except httpx.HTTPError as e:
        raise ToolError(f"unavailable: {e}") from None
    data = _decode(response)
    if response.status_code != 200:
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            raise _legacy_error(response.status_code, data["error"])
        raise ToolError(f"krita_error: /health answered HTTP {response.status_code}")
    if not isinstance(data, dict):
        raise ToolError("krita_error: /health did not return an object")
    return data


def check_protocol():
    """Raise ToolError unless the plugin speaks PROTOCOL. Once per process."""
    global _protocol_ok
    if _protocol_ok:
        return
    with _protocol_lock:
        if _protocol_ok:
            return
        reported = get_health().get("protocol")
        if reported != PROTOCOL:
            raise ToolError(protocol_mismatch_message(reported))
        _protocol_ok = True


def call(action, params=None, timeout=30.0) -> dict:
    """Run one plugin command. Returns its payload plus ``elapsed_ms``."""
    check_protocol()
    try:
        response = httpx.post(
            _url(),
            json={"action": action, "params": params or {}},
            headers=auth_headers(),
            timeout=timeout,
        )
    except httpx.ConnectError:
        raise ToolError(UNAVAILABLE) from None
    except httpx.TimeoutException:
        raise ToolError(f"timeout: Krita did not answer {action!r} within {timeout:g} s; "
                        f"the command may still finish in Krita") from None
    except httpx.HTTPError as e:
        raise ToolError(f"unavailable: {e}") from None
    return unwrap(response.status_code, _decode(response))
