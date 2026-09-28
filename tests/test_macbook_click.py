"""Second-tap click tests (POST /macbook/click).

The commit gates: fresh state feed, fresh zoom capture OF this point,
capture post-dating the positioning tap, and the live cursor still on
the point. Every miss refuses; malformed coordinates raise; the poller
fetch mirrors the mouse slot (read-only + TTL).

Run from the repo root:  python3 -m unittest tests.test_macbook_click -v
"""

import inspect
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
import macbook_map
import macbook_zoom
import touch
from touch import action_request, resolve_action

DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": -355, "y": -1080, "w": 1920, "h": 1080},
             "main": False}]
QX, QY = 464.0, -283.0  # a point on the second display
PANEL_W, PANEL_H = 1920, 1080


def state_payload(ts=None, mouse=None):
    return {
        "ts": ts if ts is not None else time.time(),
        "accessibility_trusted": True,
        "focus": {"app_name": "WezTerm",
                  "window_title": "t",
                  "window_bounds": {"x": 0, "y": -1049,
                                    "w": 1920, "h": 1049},
                  "display_index": 1},
        "mouse": {"x": QX if mouse is None else mouse[0],
                  "y": QY if mouse is None else mouse[1],
                  "display_index": 1},
        "displays": [{"bounds": dict(d["bounds"]), "main": d["main"]}
                     for d in DISPLAYS],
        "talon": {"mode": "command", "muted": False},
    }


def zoom_payload(ts=None, x=QX, y=QY):
    return {"ts": ts if ts is not None else time.time(),
            "x": x, "y": y, "jpeg": "AAA"}


def panel_of(qx, qy):
    box = macbook_map.union(DISPLAYS)
    scale, ox, oy = macbook_map.frame(
        box, PANEL_W, PANEL_H, bottom=macbook_zoom.MAP_BOTTOM)
    px, py = macbook_map.project(qx, qy, scale, ox, oy)
    return int(round(px)), int(round(py))


class DaemonClickTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        self.addCleanup(self.daemon.clear)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _armed(self, mouse=None):
        # Stage 1 first: position the cursor, then post the review
        # capture of that point (capture ts post-dates the tap).
        self.daemon.show("macbook", {})
        self.daemon.feed("macbook", "state", state_payload(mouse=mouse))
        px, py = panel_of(QX, QY)
        moved = self.daemon.request_mouse_move(px, py)
        self.assertTrue(moved["ok"], moved)
        self.daemon.feed("macbook", "zoom", zoom_payload())
        return px, py

    def test_second_tap_clicks_reviewed_point(self):
        px, py = self._armed()
        result = self.daemon.request_click_move(px, py)
        self.assertTrue(result["ok"], result)
        cmd = result["command"]
        self.assertAlmostEqual(cmd["x"], QX, delta=5)
        self.assertAlmostEqual(cmd["y"], QY, delta=5)
        self.assertIn("id", cmd)
        self.assertEqual(self.daemon.take_click_move()["id"], cmd["id"])
        self.assertIsNone(
            self.daemon.take_click_move(since=cmd["ts"]))

    def test_wrong_view_refused(self):
        self._armed()
        self.daemon.show("clock", {})
        px, py = panel_of(QX, QY)
        result = self.daemon.request_click_move(px, py)
        self.assertFalse(result["ok"])
        self.assertIn("not showing", result["reason"])

    def test_missing_or_stale_capture_refused(self):
        self.daemon.show("macbook", {})
        self.daemon.feed("macbook", "state", state_payload())
        px, py = panel_of(QX, QY)
        self.assertIn("review",
                      self.daemon.request_click_move(px, py)["reason"])
        self.daemon.feed("macbook", "zoom",
                         zoom_payload(ts=time.time() - 999))
        self.assertIn("review",
                      self.daemon.request_click_move(px, py)["reason"])

    def test_tap_off_reviewed_point_refused(self):
        self._armed()
        other = panel_of(100, 100)
        result = self.daemon.request_click_move(*other)
        self.assertFalse(result["ok"])
        self.assertIn("reviewed point", result["reason"])

    def test_moved_cursor_refused(self):
        # First tap, then the cursor wanders: the delayed second tap
        # must NOT click wherever the mouse has since moved.
        self.daemon.show("macbook", {})
        self.daemon.feed("macbook", "state", state_payload())
        px, py = panel_of(QX, QY)
        self.assertTrue(self.daemon.request_mouse_move(px, py)["ok"])
        self.daemon.feed("macbook", "zoom", zoom_payload())
        self.daemon.feed("macbook", "state",
                         state_payload(mouse=(900.0, 900.0)))
        result = self.daemon.request_click_move(px, py)
        self.assertFalse(result["ok"])
        self.assertIn("moved", result["reason"])

    def test_capture_predating_position_refused(self):
        self.daemon.show("macbook", {})
        self.daemon.feed("macbook", "state", state_payload())
        self.daemon.feed("macbook", "zoom",
                         zoom_payload(ts=time.time() - 5))
        px, py = panel_of(QX, QY)
        self.assertTrue(self.daemon.request_mouse_move(px, py)["ok"])
        result = self.daemon.request_click_move(px, py)
        self.assertFalse(result["ok"])
        self.assertIn("predates", result["reason"])

    def test_malformed_coords_raise(self):
        self._armed()
        for bad in (("1", 2), (1.5, 2), (True, 2), (-1, 2),
                    (99999, 2), (1, None)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.daemon.request_click_move(*bad)

    def test_ttl_expires_and_bad_since_is_zero(self):
        px, py = self._armed()
        cmd = self.daemon.request_click_move(px, py)["command"]
        self.assertEqual(
            self.daemon.take_click_move(since="junk")["id"], cmd["id"])
        self.daemon.click_pending["ts"] -= 60
        self.assertIsNone(self.daemon.take_click_move())
        self.assertIsNone(self.daemon.take_click_move(since=0))

    def test_http_routes(self):
        get_src = inspect.getsource(displayd.Handler.do_GET)
        post_src = inspect.getsource(displayd.Handler.do_POST)
        self.assertIn("/macbook/click", get_src)
        self.assertIn("/macbook/click", post_src)

    def test_oversize_capture_refused_by_schema(self):
        # The wire bound is enforced daemon-side, not just by sender
        # discipline: a >140000-char jpeg never enters the buffer.
        self.daemon.show("macbook", {})
        with self.assertRaises(ValueError):
            self.daemon.feed("macbook", "zoom",
                             dict(zoom_payload(),
                                  jpeg="A" * 140001))


class ZoomPaneTest(unittest.TestCase):
    def _zoom(self, ts=None, color=(40, 90, 140)):
        from PIL import Image
        import io
        import base64
        shot = Image.new("RGB", (480, 360), color)
        buf = io.BytesIO()
        shot.save(buf, "JPEG")
        return {"ts": ts if ts is not None else time.time(),
                "x": QX, "y": QY,
                "jpeg": base64.b64encode(buf.getvalue()).decode()}

    def test_fresh_capture_paints_the_pane(self):
        # Regression: the crop must land on the PANEL image (a shadowed
        # local once pasted it into itself, leaving black + crosshair).
        from PIL import Image, ImageDraw
        import macbook_zoom as mz

        class Scr:
            W, H = PANEL_W, PANEL_H
        img = Image.new("RGB", (PANEL_W, PANEL_H), (10, 10, 14))
        mz.draw(img, ImageDraw.Draw(img), Scr(), self._zoom(), None)
        # 2x of a 480-wide shot is 960 wide, centred: content at
        # (600, 900), background margins outside it, crosshair dead
        # centre on the shot centre (the positioned cursor).
        self.assertNotEqual(img.getpixel((600, 900)), (10, 10, 14))
        self.assertEqual(img.getpixel((200, 900)), (10, 10, 14))
        self.assertEqual(img.getpixel((960, 900)), mz.C_ZOOM)

    def test_stale_capture_paints_no_image(self):
        from PIL import Image, ImageDraw
        import macbook_zoom as mz

        class Scr:
            W, H = PANEL_W, PANEL_H
        img = Image.new("RGB", (PANEL_W, PANEL_H), (10, 10, 14))
        mz.draw(img, ImageDraw.Draw(img), Scr(),
                self._zoom(ts=time.time() - 999), None)
        self.assertEqual(img.getpixel((200, 900)), (10, 10, 14))


class ClickActionTest(unittest.TestCase):
    def test_table_entry_is_closed(self):
        spec = touch.ACTION_TABLE["macbook_click"]
        self.assertEqual(spec["method"], "POST")
        self.assertEqual(spec["path"], "/macbook/click")
        self.assertEqual(spec["params"], ("x", "y"))

    def test_dispatch_posts_stamped_point(self):
        resolved = resolve_action({"name": "macbook_click", "x": 960,
                                   "y": 800}, panel=(1920, 1080))
        self.assertEqual(resolved, ("POST", "/macbook/click",
                                    {"x": 960, "y": 800}))
        method, path, body = action_request(
            {"name": "macbook_click"}, allow_missing_coords=True)
        self.assertEqual((method, path, body),
                         ("POST", "/macbook/click", {}))
        self.assertIsNone(resolve_action({"name": "macbook_click"}))


if __name__ == "__main__":
    unittest.main()
