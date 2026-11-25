"""The Krita Extension: starts the bridge and drains the command queue."""

import os

from krita import Extension, Krita
from PyQt5.QtCore import QTimer

from . import bridge, config, dispatch, version


class KritaMCPExtension(Extension):
    """Main Krita extension class."""

    def __init__(self, parent):
        super().__init__(parent)
        self.server_thread = None
        self.timer = None

    def setup(self):
        """Called when extension is loaded."""
        pass

    def createActions(self, window):
        """Called when a new window is created."""
        # Cache Krita's version on the main thread; /health reports it from
        # the HTTP thread, which must not call libkis.
        if version.KRITA_VERSION is None:
            try:
                version.KRITA_VERSION = str(Krita.instance().version())
            except Exception as e:
                print(f"[KritaMCP] could not read Krita's version: {e}")

        # Ensure the output directory exists, and log config problems
        # (krita_health reports them too).
        settings = config.current()
        os.makedirs(settings.output_dir, exist_ok=True)
        for problem in settings.errors:
            print(f"[KritaMCP] config: {problem}")

        # Load (or create) the shared token before the server can accept requests
        bridge.ensure_token()

        # Start HTTP server
        if self.server_thread is None:
            self.server_thread = bridge.ServerThread(config.SERVER_PORT)
            self.server_thread.start()
            print(f"[KritaMCP] HTTP server started on port {config.SERVER_PORT}")

        # Start timer to process command queue
        if self.timer is None:
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_commands)
            self.timer.start(50)  # Check every 50ms

    def process_commands(self):
        """Run the next queued command, on the main thread (one per tick)."""
        item = bridge.command_queue.next()
        if item is None:
            return
        command_id, command = item
        envelope = None
        try:
            envelope = dispatch.execute(command)  # never raises; the D2 envelope
        finally:
            # finish() also clears busy, so it must run whatever happens.
            bridge.command_queue.finish(command_id, envelope or {
                "ok": False, "error": {"code": "krita_error", "message": "command failed"},
                "elapsed_ms": 0})
