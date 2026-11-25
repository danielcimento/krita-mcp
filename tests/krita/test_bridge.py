"""Headless checks of the HTTP handler's auth, on a throwaway port.

Uses a plain HTTPServer in a Python thread on an ephemeral port, never the
plugin's port 5678 (the GUI Krita may be using it). Only requests that are
refused before queueing are sent, so nothing waits on the main thread.
"""

import http.client
import json
import threading
import unittest
from http.server import HTTPServer

from kritamcp import bridge, config


class HandlerAuthTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), bridge.PaintRequestHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.saved_token = bridge.AUTH_TOKEN

    def tearDown(self):
        bridge.AUTH_TOKEN = self.saved_token
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            # Host must match config.ALLOWED_HOSTS, which names the real port.
            hdrs = {"Host": "localhost:%d" % config.SERVER_PORT}
            hdrs.update(headers or {})
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode())
        finally:
            conn.close()

    def test_health_needs_no_token(self):
        bridge.AUTH_TOKEN = None
        self.assertEqual(self.request("GET", "/health"),
                         (200, {"status": "ok", "plugin": "kritamcp"}))

    def test_no_token_loaded_refuses_info_and_post(self):
        bridge.AUTH_TOKEN = None
        err = {"error": "Missing or invalid token"}
        self.assertEqual(self.request("GET", "/info"), (401, err))
        self.assertEqual(self.request("GET", "/info", {config.TOKEN_HEADER: ""}), (401, err))
        self.assertEqual(self.request("POST", "/", {config.TOKEN_HEADER: "anything",
                                                    "Content-Type": "application/json"},
                                      b'{"action": "undo"}'), (401, err))
        self.assertEqual(bridge.command_queue.pop(), None)

    def test_wrong_token_refused_right_token_accepted(self):
        bridge.AUTH_TOKEN = "test-token"
        self.assertEqual(self.request("GET", "/info", {config.TOKEN_HEADER: "nope"})[0], 401)
        status, body = self.request("GET", "/info", {config.TOKEN_HEADER: "test-token"})
        self.assertEqual(status, 200)
        self.assertEqual(body["commands"], [
            "new_canvas", "set_color", "set_brush", "stroke",
            "fill", "draw_shape", "get_canvas", "undo", "redo",
            "clear", "save", "get_color_at", "list_brushes", "open_file"])

    def test_forbidden_host_and_browser_headers(self):
        bridge.AUTH_TOKEN = "test-token"
        self.assertEqual(self.request("GET", "/health", {"Host": "evil.example:5678"}),
                         (403, {"error": "Forbidden host"}))
        self.assertEqual(self.request("GET", "/health", {"Origin": "http://x"}),
                         (403, {"error": "Browser requests are not allowed"}))
