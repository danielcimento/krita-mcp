"""dispatch.execute called directly, headless (no window, so no view)."""

import unittest

from krita import Krita

from kritamcp import dispatch


def run(action, **params):
    return dispatch.execute({"action": action, "params": params})


class DispatchTest(unittest.TestCase):
    def test_unknown_action(self):
        self.assertEqual(run("nope"), {"error": "Unknown action: nope"})
        self.assertEqual(dispatch.execute({}), {"error": "Unknown action: None"})

    def test_exceptions_become_error_dicts(self):
        # params that isn't a dict makes the handler raise; execute catches it.
        result = dispatch.execute({"action": "new_canvas", "params": None})
        self.assertEqual(set(result), {"error"})

    def test_new_canvas_width_zero(self):
        self.assertEqual(run("new_canvas", width=0, height=48),
                         {"error": "'width' must be between 1 and 8192"})

    def test_new_canvas_too_many_pixels(self):
        self.assertEqual(run("new_canvas", width=8192, height=8192),
                         {"error": "Canvas too large (max 16777216 pixels)"})

    def test_new_canvas_returns_ok(self):
        before = list(Krita.instance().documents())
        try:
            result = run("new_canvas", width=64, height=48, name="kmcp-dispatch-test")
            self.assertEqual(result, {"status": "ok", "width": 64, "height": 48,
                                      "name": "kmcp-dispatch-test"})
            # Headless there is no window, so no view is added. The document
            # is owned by the Python wrapper createDocument returned, and is
            # destroyed when cmd_new_canvas returns and drops it. So nothing
            # stays open (pinned; a handle registry must keep the wrapper).
            created = [d for d in Krita.instance().documents()
                       if not any(d == b for b in before)]
            self.assertEqual(created, [])
        finally:
            # Defensive: close anything new without touching it otherwise.
            for d in Krita.instance().documents():
                if not any(d == b for b in before):
                    try:
                        d.setModified(False)
                    finally:
                        d.close()

    def test_list_brushes(self):
        result = run("list_brushes")
        self.assertEqual(result["status"], "ok")
        self.assertIsInstance(result["brushes"], list)
        self.assertEqual(result["count"], len(result["brushes"]))
        self.assertLessEqual(result["count"], 50)

    def test_list_brushes_bad_limit(self):
        self.assertEqual(run("list_brushes", limit=0),
                         {"error": "'limit' must be between 1 and 1000"})

    def test_save_outside_allowed_dirs(self):
        self.assertEqual(run("save", path="/etc/kmcp-test.png"),
                         {"error": "Path is outside the allowed directories: /etc/kmcp-test.png"})

    def test_save_no_path(self):
        self.assertEqual(run("save"), {"error": "No path specified"})

    def test_open_file_bad_extension(self):
        self.assertEqual(run("open_file", path="notes.txt"),
                         {"error": "File type not allowed: notes.txt"})

    def test_view_commands_need_a_view(self):
        self.assertEqual(run("set_color", color="#ff0000"), {"error": "No active view"})
        self.assertEqual(run("set_brush", size=5), {"error": "No active view"})

    def test_stroke_validation_before_view(self):
        self.assertEqual(run("stroke", points=[[0, 0]]),
                         {"error": "Need at least 2 points for a stroke"})
