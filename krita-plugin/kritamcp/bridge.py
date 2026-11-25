"""HTTP bridge: request handler, auth, command queue, server thread.

POST answers with the command's envelope (D2): HTTP 200 when ``ok``, else
the status for its error code (``errors.HTTP_STATUS``). Requests refused
before a command runs (auth, Host/browser, body size, Content-Type, bad
JSON, unknown endpoint) keep the plain ``{"error": "<string>"}`` form.

The HTTP thread never calls libkis (``krita``); only the main thread does.

Nothing here has side effects at import. The shared token is loaded by
``ensure_token()``, which the extension calls at startup; until then
``AUTH_TOKEN`` is None and every token-protected request gets a 401.
"""

import hmac
import json
import os
import secrets
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from PyQt5.QtCore import QThread

from . import config, dispatch, version
from .errors import HTTP_STATUS

# Set by ensure_token() at extension startup. None = refuse authenticated requests.
AUTH_TOKEN = None

# How long do_POST waits for the main thread. (P1.5 replaces this with the
# per-command timeouts from the dispatch registry.)
RESULT_TIMEOUT_S = 120


def load_or_create_token():
    """Read the shared token, creating it (mode 0600) if it does not exist."""
    try:
        fd = os.open(config.TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(config.TOKEN_FILE) as f:
            return f.read().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def ensure_token():
    """Load (or create) the shared token once and return it."""
    global AUTH_TOKEN
    if AUTH_TOKEN is None:
        AUTH_TOKEN = load_or_create_token()
    return AUTH_TOKEN


class CommandQueue:
    """Thread-safe command queue for passing commands from HTTP thread to main thread."""
    def __init__(self):
        self.queue = []
        self.results = {}
        self.lock = threading.Lock()
        self.result_event = threading.Event()

    def push(self, command_id, command):
        with self.lock:
            self.queue.append((command_id, command))

    def pop(self):
        with self.lock:
            if self.queue:
                return self.queue.pop(0)
            return None

    def set_result(self, command_id, result):
        with self.lock:
            self.results[command_id] = result
        self.result_event.set()

    def get_result(self, command_id, timeout=120):
        """Wait for result with timeout.

        The default timeout of 120s is important — canvas export and save
        operations can take a long time on large canvases. The original 30s
        default caused frequent timeouts. The MCP server's httpx timeout
        (krita_mcp_server/client.py) must match or exceed this value.
        """
        start = threading.Event()
        for _ in range(int(timeout * 10)):  # Check every 100ms
            with self.lock:
                if command_id in self.results:
                    result = self.results.pop(command_id)
                    return result
            self.result_event.wait(0.1)
            self.result_event.clear()
        return {"ok": False, "error": {
            "code": "timeout",
            "message": f"Timed out after {timeout:g} s waiting for the command to run; "
                       "it may still finish later"}}

# Global command queue
command_queue = CommandQueue()
command_counter = 0

def config_health():
    """The config fields of ``/health``. Never raises: if loading the config
    fails unexpectedly, the failure is reported in ``config_errors``.

    ``/health`` needs no token, so any local process can read these
    (directory names only; the server listens on 127.0.0.1 and refuses
    browser requests). Pure Python and ``os.stat``: safe on the HTTP thread.
    """
    try:
        s = config.current()
        return {
            "allowed_dirs": list(s.roots),  # [0] is the output dir
            "config_file": s.config_file,
            "config_loaded": s.config_loaded,
            "limits": dict(s.limits),
            "config_errors": list(s.errors),
        }
    except Exception as e:
        return {"allowed_dirs": [], "config_file": None, "config_loaded": False, "limits": None,
                "config_errors": [f"could not load the config: {type(e).__name__}: {e}"]}


def envelope_status(envelope):
    """HTTP status for a command envelope: 200 if ok, else per its error code."""
    if envelope.get("ok"):
        return 200
    code = (envelope.get("error") or {}).get("code")
    return HTTP_STATUS.get(code, 500)


class PaintRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for paint commands."""

    def log_message(self, format, *args):
        # Suppress HTTP logging
        pass

    def send_json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def reject_untrusted(self, require_token=True):
        """Return True (after sending an error) if the request should be refused.

        Blocks browser-originated requests (CSRF), DNS rebinding (Host check),
        and anything without the shared token (always refused while no token
        is loaded).
        """
        host = self.headers.get('Host', '')
        if host not in config.ALLOWED_HOSTS:
            self.send_json_response({"error": "Forbidden host"}, 403)
            return True
        if self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site'):
            self.send_json_response({"error": "Browser requests are not allowed"}, 403)
            return True
        if require_token:
            supplied = self.headers.get(config.TOKEN_HEADER, '')
            token = AUTH_TOKEN
            if token is None or not hmac.compare_digest(supplied.encode(), token.encode()):
                self.send_json_response({"error": "Missing or invalid token"}, 401)
                return True
        return False

    def discard_body(self):
        """Read and drop an unread request body after an early refusal.

        Closing the socket with unread data in it makes the OS send a TCP
        reset, and the client then sees "connection reset" instead of our
        401/403/415 reply. Bodies over the size limit are not read.
        """
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            return
        if not 0 < length <= config.MAX_BODY_BYTES:
            return
        try:
            self.connection.settimeout(2.0)
            self.rfile.read(length)
        except OSError:
            pass

    def do_GET(self):
        """Handle GET requests - mainly for health check."""
        parsed = urlparse(self.path)

        if parsed.path == '/health':
            if self.reject_untrusted(require_token=False):
                return
            self.send_json_response({
                "status": "ok",
                "plugin": "kritamcp",
                "protocol": version.PROTOCOL,
                "version": version.PLUGIN_VERSION,
                "krita_version": version.KRITA_VERSION,  # cached at startup
                **config_health(),
            })
        elif parsed.path == '/info':
            if self.reject_untrusted():
                return
            self.send_json_response({
                "status": "ok",
                "protocol": version.PROTOCOL,
                "commands": dispatch.actions(),
            })
        else:
            self.send_json_response({"error": "Unknown endpoint"}, 404)

    def do_POST(self):
        """Handle POST requests - paint commands."""
        global command_counter

        if self.reject_untrusted():
            self.discard_body()
            return

        content_type = self.headers.get('Content-Type', '')
        if content_type.split(';')[0].strip().lower() != 'application/json':
            self.send_json_response({"error": "Content-Type must be application/json"}, 415)
            self.discard_body()
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            self.send_json_response({"error": "Invalid Content-Length"}, 400)
            return
        if content_length < 0 or content_length > config.MAX_BODY_BYTES:
            self.send_json_response({"error": "Request body too large"}, 413)
            return

        try:
            body = self.rfile.read(content_length).decode('utf-8')
            command = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json_response({"error": "Invalid JSON"}, 400)
            return
        if not isinstance(command, dict) or not isinstance(command.get("params", {}), dict):
            self.send_json_response({"error": "Command must be a JSON object"}, 400)
            return

        # Assign command ID and queue it
        command_counter += 1
        command_id = command_counter
        command_queue.push(command_id, command)

        # Wait for result from main thread
        result = command_queue.get_result(command_id, timeout=RESULT_TIMEOUT_S)
        self.send_json_response(result, envelope_status(result))


class ServerThread(QThread):
    """Thread to run HTTP server without blocking Krita UI."""

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.server = None

    def run(self):
        self.server = HTTPServer(('127.0.0.1', self.port), PaintRequestHandler)
        self.server.serve_forever()

    def stop(self):
        if self.server:
            self.server.shutdown()
