"""Headless checks of the HTTP handler, on a throwaway port.

Uses the plugin's threaded server (``bridge.BridgeServer``) in a Python
thread on an ephemeral port, never the plugin's port 5678 (the GUI Krita may
be using it). Each test gets a fresh ``bridge.command_queue``. For requests
that queue a command, the test thread plays the extension's timer: it takes
the command with ``next()`` and runs ``dispatch.execute`` on Krita's main
thread, as the plugin does.
"""

import http.client
import json
import threading
import time
import unittest

from helpers import temp_config
from kritamcp import bridge, cmdqueue, config, dispatch, version
from test_dispatch import EXPECTED_REGISTRY

TOKEN = "test-token"
JSON = {config.TOKEN_HEADER: TOKEN, "Content-Type": "application/json"}


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.saved = (bridge.AUTH_TOKEN, bridge.command_queue)
        bridge.command_queue = cmdqueue.CommandQueue()  # nothing leaks between tests
        self.server = bridge.BridgeServer(("127.0.0.1", 0), bridge.PaintRequestHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        bridge.AUTH_TOKEN, bridge.command_queue = self.saved

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

    def send(self, command):
        """POST ``command`` from a background thread. Returns ``(thread, out)``;
        after ``thread.join``, ``reply(out)`` is the ``(status, body)``."""
        body = command if isinstance(command, bytes) else json.dumps(command).encode()
        out = {}

        def run():
            try:
                out["reply"] = self.request("POST", "/", JSON, body)
            except Exception as e:  # surfaced by reply()
                out["exc"] = e

        sender = threading.Thread(target=run, daemon=True)
        sender.start()
        return sender, out

    @staticmethod
    def reply(out):
        if "exc" in out:
            raise out["exc"]
        return out["reply"]

    def take(self, sender):
        """Wait (as the main thread) for the next queued command; None if the
        sender finished without one being queued."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            item = bridge.command_queue.next()
            if item is not None or not sender.is_alive():
                return item
            time.sleep(0.005)
        return None

    def post(self, command, pump=True):
        """POST a command; with ``pump``, execute it here as the main thread would."""
        sender, out = self.send(command)
        if pump:
            item = self.take(sender)
            if item is not None:
                command_id, cmd = item
                bridge.command_queue.finish(command_id, dispatch.execute(cmd))
        sender.join(15)
        return self.reply(out)


class HandlerAuthTest(BridgeTestBase):
    def test_health_needs_no_token(self):
        bridge.AUTH_TOKEN = None
        settings = config.current()  # the runner's: no config file, so defaults
        self.assertEqual(self.request("GET", "/health"), (200, {
            "status": "ok", "plugin": "kritamcp", "protocol": 3,
            "version": version.PLUGIN_VERSION,
            "krita_version": None,  # cached by createActions, which never runs headless
            "allowed_dirs": [settings.output_dir],
            "config_file": settings.config_file,
            "config_exists": False,
            "config_loaded": False,
            "config_key": "allowed_dirs",
            "config_dirs": [],
            "allowed_dirs_env": "KRITA_MCP_ALLOWED_DIRS",
            "config_reload": config.RELOAD_NOTE,
            "limits": config.DEFAULT_LIMITS,
            "lint": config.DEFAULT_LINT,
            "config_errors": [],
            "busy": None,
            "queued": 0,
        }))

    def test_health_reports_config(self):
        bridge.AUTH_TOKEN = None
        with temp_config({"allowed_dirs": ["/definitely/not/here", "relative"],
                          "limits": {"max_canvas_pixels": 4096}, "colour": 1,
                          "lint": {"additive_mode": "add"}}) as path:
            status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["config_file"], path)
        self.assertIs(body["config_loaded"], True)
        self.assertEqual(body["allowed_dirs"], [config.current().output_dir])
        self.assertEqual(body["limits"], {"max_canvas_dim": 16384, "max_canvas_pixels": 4096})
        self.assertEqual(body["lint"], {"additive_mode": "add", "stray_alpha_below": 8,
                                        "stray_alpha_distance": 4, "stray_speck_max_px": 64})
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
        self.assertIsNone(body["lint"])
        self.assertEqual(body["config_errors"], ["could not load the config: RuntimeError: boom"])

    def test_no_token_loaded_refuses_info_and_post(self):
        bridge.AUTH_TOKEN = None
        err = {"error": "Missing or invalid token"}
        self.assertEqual(self.request("GET", "/info"), (401, err))
        self.assertEqual(self.request("GET", "/info", {config.TOKEN_HEADER: ""}), (401, err))
        self.assertEqual(self.request("POST", "/", {config.TOKEN_HEADER: "anything",
                                                    "Content-Type": "application/json"},
                                      b'{"action": "undo"}'), (401, err))
        self.assertEqual(bridge.command_queue.next(), None)

    def test_wrong_token_refused_right_token_accepted(self):
        bridge.AUTH_TOKEN = TOKEN
        self.assertEqual(self.request("GET", "/info", {config.TOKEN_HEADER: "nope"})[0], 401)
        status, body = self.request("GET", "/info", {config.TOKEN_HEADER: TOKEN})
        self.assertEqual(status, 200)
        names = dispatch.actions()
        self.assertEqual(body, {
            "status": "ok", "protocol": 3, "commands": names,
            "timeouts": {n: dispatch.get(n).timeout for n in names},
            "mutating": [n for n in names if dispatch.get(n).mutating],
        })
        self.assertEqual(body["commands"], sorted(body["commands"]))
        self.assertEqual(body["commands"], sorted(EXPECTED_REGISTRY))
        self.assertEqual(body["timeouts"]["save"], dispatch.LONG_TIMEOUT)
        self.assertEqual(body["timeouts"]["undo"], dispatch.LONG_TIMEOUT)
        self.assertEqual(body["timeouts"]["list_layers"], dispatch.QUERY_TIMEOUT)
        self.assertIn("new_canvas", body["mutating"])
        self.assertNotIn("set_color", body["mutating"])

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
        self.assertEqual(bridge.command_queue.next(), None)


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
        self.assertEnvelopeError(self.post({"action": "set_brush", "params": {"preset": "Basic"}}),
                                 409, "conflict")

    def test_not_found_is_404(self):
        self.assertEnvelopeError(self.post({"action": "get_color_at",
                                             "params": {"x": 0, "y": 0, "doc": "doc-999999"}}),
                                 404, "not_found")

    def test_bad_request_is_400(self):
        self.assertEnvelopeError(self.post({"action": "new_canvas", "params": {"width": 0}}),
                                 400, "bad_request")
        self.assertEnvelopeError(self.post({"action": "nope"}), 400, "bad_request")

    def test_too_large_is_413(self):
        self.assertEnvelopeError(
            self.post({"action": "new_canvas", "params": {"width": 9000, "height": 9000}}),
            413, "too_large")

    def test_timeout_before_start_is_504_and_skipped(self):
        status, body = self.post({"action": "undo", "params": {"timeout_s": 0.2}}, pump=False)
        self.assertEqual(status, 504)
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error"]["code"], "timeout")
        self.assertEqual(body["error"]["details"],
                         {"action": "undo", "timeout_s": 0.2, "started": False})
        self.assertEqual(bridge.command_queue.next(), None)  # it will never run

    def test_timeout_while_running_is_504_and_result_dropped(self):
        sender, out = self.send({"action": "undo", "params": {"timeout_s": 0.5}})
        command_id, cmd = self.take(sender)  # "running" until finish()
        sender.join(15)
        status, body = self.reply(out)
        self.assertEqual(status, 504)
        self.assertEqual(body["error"]["code"], "timeout")
        self.assertEqual(body["error"]["details"],
                         {"action": "undo", "timeout_s": 0.5, "started": True})
        bridge.command_queue.finish(command_id, {"ok": True, "result": {}, "elapsed_ms": 1})
        self.assertEqual(bridge.command_queue._results, {})
        self.assertEqual(bridge.command_queue.status(), {"busy": None, "queued": 0})

    def test_timeout_s_is_removed_before_the_handler(self):
        sender, out = self.send({"action": "list_brushes",
                                 "params": {"limit": 2, "timeout_s": 5}})
        command_id, cmd = self.take(sender)
        self.assertEqual(cmd, {"action": "list_brushes", "params": {"limit": 2}})
        bridge.command_queue.finish(command_id, dispatch.execute(cmd))
        sender.join(15)
        self.assertEqual(self.reply(out)[0], 200)

    def test_bad_timeout_s_is_400_and_not_queued(self):
        for value in (0, -1, 901, "x", float("nan"), True):
            body = json.dumps({"action": "undo", "params": {"timeout_s": value}}).encode()
            status, reply = self.request("POST", "/", JSON, body)
            self.assertEqual(status, 400, value)
            self.assertIs(reply["ok"], False)
            self.assertEqual(reply["error"]["code"], "bad_request")
            self.assertIn("timeout_s", reply["error"]["message"])
            self.assertEqual(bridge.command_queue.status(), {"busy": None, "queued": 0})

    def test_health_answers_while_a_command_runs(self):
        sender, out = self.send({"action": "save", "params": {"path": "x.png"}})
        command_id, cmd = self.take(sender)
        queued, queued_out = self.send({"action": "undo"})
        deadline = time.monotonic() + 10
        while bridge.command_queue.status()["queued"] == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        # The POST handler is still blocked waiting; /health must not be.
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["busy"]["action"], "save")
        self.assertIsInstance(health["busy"]["elapsed_ms"], int)
        self.assertEqual(health["queued"], 1)
        self.assertTrue(sender.is_alive())
        bridge.command_queue.finish(command_id, {"ok": True, "result": {"path": "x"},
                                                 "elapsed_ms": 3})
        sender.join(15)
        self.assertEqual(self.reply(out), (200, {"ok": True, "result": {"path": "x"},
                                                 "elapsed_ms": 3}))
        command_id, cmd = self.take(queued)
        self.assertEqual(cmd, {"action": "undo"})
        bridge.command_queue.finish(command_id, {"ok": True, "result": {}, "elapsed_ms": 0})
        queued.join(15)
        self.assertEqual(self.reply(queued_out)[0], 200)
        self.assertEqual(self.request("GET", "/health")[1]["busy"], None)

    def test_envelope_status(self):
        self.assertEqual(bridge.envelope_status({"ok": True, "result": {}}), 200)
        self.assertEqual(bridge.envelope_status({"ok": False, "error": {"code": "ambiguous"}}), 409)
        self.assertEqual(bridge.envelope_status({"ok": False, "error": {"code": "?"}}), 500)
