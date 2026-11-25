"""Headless checks of the HTTP handler, on a throwaway port.

Uses a plain HTTPServer in a Python thread on an ephemeral port, never the
plugin's port 5678 (the GUI Krita may be using it). For requests that queue
a command, the test thread plays the extension's timer: it pops the command
and runs ``dispatch.execute`` on Krita's main thread, as the plugin does.
"""

import http.client
import json
import threading
import time
import unittest
from http.server import HTTPServer

from helpers import temp_config
from kritamcp import bridge, config, dispatch, version

TOKEN = "test-token"
JSON = {config.TOKEN_HEADER: TOKEN, "Content-Type": "application/json"}


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), bridge.PaintRequestHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.saved = (bridge.AUTH_TOKEN, bridge.command_counter, bridge.RESULT_TIMEOUT_S)

    def tearDown(self):
        bridge.AUTH_TOKEN, bridge.command_counter, bridge.RESULT_TIMEOUT_S = self.saved
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        while bridge.command_queue.pop() is not None:  # nothing may leak to other tests
            pass
        with bridge.command_queue.lock:
            bridge.command_queue.results.clear()

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

    def post(self, command, pump=True):
        """POST a command; with ``pump``, execute it here as the main thread would."""
        body = command if isinstance(command, bytes) else json.dumps(command).encode()
        out = {}

        def send():
            try:
                out["reply"] = self.request("POST", "/", JSON, body)
            except Exception as e:  # surfaced below
                out["exc"] = e

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        if pump:
            deadline = time.monotonic() + 10
            item = None
            while item is None and sender.is_alive() and time.monotonic() < deadline:
                item = bridge.command_queue.pop()
                if item is None:
                    time.sleep(0.01)
            if item is not None:
                command_id, cmd = item
                bridge.command_queue.set_result(command_id, dispatch.execute(cmd))
        sender.join(15)
        if "exc" in out:
            raise out["exc"]
        return out["reply"]


class HandlerAuthTest(BridgeTestBase):
    def test_health_needs_no_token(self):
        bridge.AUTH_TOKEN = None
        settings = config.current()  # the runner's: no config file, so defaults
        self.assertEqual(self.request("GET", "/health"), (200, {
            "status": "ok", "plugin": "kritamcp", "protocol": 2,
            "version": version.PLUGIN_VERSION,
            "krita_version": None,  # cached by createActions, which never runs headless
            "allowed_dirs": [settings.output_dir],
            "config_file": settings.config_file,
            "config_loaded": False,
            "limits": config.DEFAULT_LIMITS,
            "config_errors": [],
        }))

    def test_health_reports_config(self):
        bridge.AUTH_TOKEN = None
        with temp_config({"allowed_dirs": ["/definitely/not/here", "relative"],
                          "limits": {"max_canvas_pixels": 4096}, "colour": 1}) as path:
            status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["config_file"], path)
        self.assertIs(body["config_loaded"], True)
        self.assertEqual(body["allowed_dirs"], [config.current().output_dir])
        self.assertEqual(body["limits"], {"max_canvas_dim": 16384, "max_canvas_pixels": 4096})
        errors = body["config_errors"]
        self.assertEqual(len(errors), 3, errors)
        self.assertTrue(any("'colour'" in e for e in errors), errors)
        self.assertTrue(any("'/definitely/not/here' skipped: does not exist" in e for e in errors))
        self.assertTrue(any("'relative' skipped: not an absolute path" in e for e in errors))

    def test_health_answers_if_config_fails(self):
        bridge.AUTH_TOKEN = None
        real = config.current

        def broken():
            raise RuntimeError("boom")

        config.current = broken
        try:
            status, body = self.request("GET", "/health")
        finally:
            config.current = real
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["allowed_dirs"], [])
        self.assertEqual(body["config_errors"], ["could not load the config: RuntimeError: boom"])

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
        bridge.AUTH_TOKEN = TOKEN
        self.assertEqual(self.request("GET", "/info", {config.TOKEN_HEADER: "nope"})[0], 401)
        status, body = self.request("GET", "/info", {config.TOKEN_HEADER: TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "protocol": 2, "commands": dispatch.actions()})
        self.assertEqual(body["commands"], sorted(body["commands"]))
        self.assertEqual(len(body["commands"]), 14)

    def test_forbidden_host_and_browser_headers(self):
        bridge.AUTH_TOKEN = TOKEN
        self.assertEqual(self.request("GET", "/health", {"Host": "evil.example:5678"}),
                         (403, {"error": "Forbidden host"}))
        self.assertEqual(self.request("GET", "/health", {"Origin": "http://x"}),
                         (403, {"error": "Browser requests are not allowed"}))

    def test_transport_errors_keep_legacy_form(self):
        bridge.AUTH_TOKEN = TOKEN
        self.assertEqual(self.request("GET", "/nope"), (404, {"error": "Unknown endpoint"}))
        self.assertEqual(self.request("POST", "/", {config.TOKEN_HEADER: TOKEN,
                                                    "Content-Type": "text/plain"}, b"{}"),
                         (415, {"error": "Content-Type must be application/json"}))
        self.assertEqual(self.request("POST", "/", JSON, b"{not json"),
                         (400, {"error": "Invalid JSON"}))
        self.assertEqual(self.request("POST", "/", JSON, b'{"action": "undo", "params": 3}'),
                         (400, {"error": "Command must be a JSON object"}))
        self.assertEqual(bridge.command_queue.pop(), None)


class PostEnvelopeTest(BridgeTestBase):
    def setUp(self):
        super().setUp()
        bridge.AUTH_TOKEN = TOKEN

    def assertEnvelopeError(self, reply, status, code):
        got_status, body = reply
        self.assertEqual(got_status, status, body)
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["elapsed_ms"], int)

    def test_ok_is_200(self):
        status, body = self.post({"action": "list_brushes", "params": {"limit": 3}})
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(set(body["result"]), {"brushes", "count"})
        self.assertIsInstance(body["elapsed_ms"], int)

    def test_conflict_is_409(self):
        self.assertEnvelopeError(self.post({"action": "set_color", "params": {"color": "#f00"}}),
                                 409, "conflict")

    def test_not_found_is_404(self):
        self.assertEnvelopeError(self.post({"action": "get_color_at", "params": {}}),
                                 404, "not_found")

    def test_bad_request_is_400(self):
        self.assertEnvelopeError(self.post({"action": "new_canvas", "params": {"width": 0}}),
                                 400, "bad_request")
        self.assertEnvelopeError(self.post({"action": "nope"}), 400, "bad_request")

    def test_too_large_is_413(self):
        self.assertEnvelopeError(
            self.post({"action": "new_canvas", "params": {"width": 9000, "height": 9000}}),
            413, "too_large")

    def test_wait_timeout_is_504(self):
        bridge.RESULT_TIMEOUT_S = 0.3
        status, body = self.post({"action": "undo"}, pump=False)
        self.assertEqual(status, 504)
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error"]["code"], "timeout")
        # The command is still queued (P1.5 adds abandonment); tearDown drops it.
        self.assertIsNotNone(bridge.command_queue.pop())

    def test_envelope_status(self):
        self.assertEqual(bridge.envelope_status({"ok": True, "result": {}}), 200)
        self.assertEqual(bridge.envelope_status({"ok": False, "error": {"code": "ambiguous"}}), 409)
        self.assertEqual(bridge.envelope_status({"ok": False, "error": {"code": "?"}}), 500)
