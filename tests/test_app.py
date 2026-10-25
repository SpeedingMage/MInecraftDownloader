"""Exercise selection, filtering, settings and worker-event delivery in Tk."""
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Application, fitted_geometry


class AppTests(unittest.TestCase):
    def test_default_geometry_stays_inside_work_area(self):
        for area in ((0, 0, 1920, 1040), (0, 0, 1536, 816),
                     (0, 0, 1280, 680), (40, 0, 1024, 600), (0, 32, 800, 480)):
            with self.subTest(area=area):
                width, height, x, y, _, _ = fitted_geometry(area)
                self.assertLessEqual(width, 960)
                self.assertLessEqual(height, 660)
                self.assertGreaterEqual(x, area[0])
                self.assertGreaterEqual(y, area[1])
                self.assertLessEqual(x + width + 8, area[2])
                self.assertLessEqual(y + height + 40, area[3])

    def test_controls_remain_visible_in_small_scaled_windows(self):
        for scaling in (1.5, 2.0, 2.5):
            with self.subTest(scaling=scaling):
                root = tk.Tk()
                try:
                    root.tk.call("tk", "scaling", scaling)
                    with patch("app.desktop_work_area", return_value=(0, 0, 1280, 720)):
                        app = Application(root, auto_refresh=False)
                    root.geometry("720x480+10+10")
                    root.update()
                    self.assertTrue(app.narrow_layout)
                    bounds = app.content_canvas.bbox("all")
                    self.assertGreater(bounds[3], app.content_canvas.winfo_height())
                    for logs in (False, True):
                        if logs != app.show_logs:
                            app.toggle_logs()
                        app.show_progress((4, 10, "libraries/" + "long-name" * 20, 2, 300))
                        root.update()
                        before = app.start_button.winfo_rooty()
                        app.content_canvas.yview_moveto(1)
                        root.update()
                        self.assertEqual(before, app.start_button.winfo_rooty())
                        for widget in (app.start_button, app.cancel_button, app.open_button, app.log_button):
                            self.assertGreaterEqual(widget.winfo_rootx(), root.winfo_rootx())
                            self.assertGreaterEqual(widget.winfo_rooty(), root.winfo_rooty())
                            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), root.winfo_rootx() + root.winfo_width())
                            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), root.winfo_rooty() + root.winfo_height())
                        # Scroll the desired setting into view, rather than requiring
                        # it to remain visible when scrolling past it to the footnote.
                        setting_y = 0
                        widget = app.pack_button
                        while widget is not app.columns:
                            setting_y += widget.winfo_y()
                            widget = widget.master
                        region_height = app.content_canvas.bbox("all")[3]
                        app.content_canvas.yview_moveto(max(0, setting_y - 4) / region_height)
                        root.update()
                        self.assertGreaterEqual(app.pack_button.winfo_rooty(), app.content_canvas.winfo_rooty())
                        self.assertLessEqual(app.pack_button.winfo_rooty() + app.pack_button.winfo_height(),
                                             app.content_canvas.winfo_rooty() + app.content_canvas.winfo_height())
                finally:
                    root.destroy()

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
            app.output.set(str(Path.cwd() / "preview/.minecraft"))
            self.assertIn("增量", app.destination_mode.get())
            self.assertNotIn("Minecraft-1.21.1", app.destination_preview.get())
            app.output.set(str(Path.cwd() / "preview/Games"))
            self.assertIn("独立实例", app.destination_mode.get())
            self.assertIn("Minecraft-1.21.1", app.destination_preview.get())
            app.toggle_logs()
            self.assertTrue(app.show_logs)
            root.geometry("1040x810")
            root.update()
            for widget in (app.start_button, app.pack_button, app.proxy_entry, app.worker_spin):
                parent = widget.nametowidget(widget.winfo_parent())
                self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), parent.winfo_height())
                self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), parent.winfo_width())
            app.toggle_logs()
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
            self.assertEqual(app.reuse_metric.get(), "2")
            self.assertEqual(app.percent.get(), "40%")
            self.assertFalse(app.busy)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
