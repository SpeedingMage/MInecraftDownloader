"""Exercise selection, filtering, settings and worker-event delivery in Tk."""
from pathlib import Path
import sys
import tkinter as tk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Application


class AppTests(unittest.TestCase):
    def test_window_filters_and_progress_events(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = Application(root, auto_refresh=False)
            app.versions = [
                {"id": "1.21.1", "type": "release", "releaseTime": "2024-08-08"},
                {"id": "test-snapshot", "type": "snapshot", "releaseTime": "2024-08-09"},
                {"id": "a1.0", "type": "old_alpha", "releaseTime": "2010-01-01"},
            ]
            app.filter_versions()
            root.update()
            self.assertEqual(len(app.visible), 1)
            self.assertIn("1.21.1", app.selection.get())
            app.snapshots.set(True)
            app.filter_versions()
            self.assertEqual(len(app.visible), 2)
            app.search.set("snapshot")
            root.update()
            self.assertEqual(app.visible[0]["id"], "test-snapshot")
            app.search.set("not-found")
            self.assertEqual(len(app.visible), 0)
            app.proxy_mode.set("自定义 HTTP 代理")
            app.sync_proxy()
            self.assertEqual(str(app.proxy_entry["state"]), "normal")
            app.set_busy(True)
            self.assertEqual(str(app.proxy_entry["state"]), "disabled")
            app.events.put(("total", (10, 1000)))
            app.events.put(("progress", (4, 10, "libraries/test.jar", 2, 300)))
            app.events.put(("idle", None))
            app.poll()
            self.assertEqual(float(app.progress["value"]), 4)
            self.assertIn("4 / 10", app.status.get())
            self.assertFalse(app.busy)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
