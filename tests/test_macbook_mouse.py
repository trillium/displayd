"""Panel tap -> MacBook cursor (macbook_mouse) tests.

Three levels: the pure inverse geometry (macbook_map.frame/unproject/
locate, including map-then-inverse round-trips and the multi-display
case), the daemon slot (POST /macbook/mouse validation, view gating,
fresh-feed gating, queue + TTL fetch), and the Mac-side poller helpers
(fetch shape tolerance, direct Quartz warp).

Run from the repo root:  python3 -m unittest tests.test_macbook_mouse -v
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
import macbook_layout
import macbook_map
import macos_state

DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": -355, "y": -1080, "w": 1920, "h": 1080},
             "main": False}]
PANEL_W, PANEL_H = 1920, 1080


def feed_payload(ts=None):
    return {
        "ts": ts if ts is not None else time.time(),
        "accessibility_trusted": True,
        "focus": {"app_name": "WezTerm",
                  "window_title": "macbookpro: fm-",
                  "window_bounds": {"x": 0, "y": -1049,
                                    "w": 1920, "h": 1049},
                  "display_index": 1},
        "mouse": {"x": 464, "y": -283, "display_index": 1},
        "displays": [{"bounds": dict(d["bounds"]), "main": d["main"]}
                     for d in DISPLAYS],
        "talon": {"mode": "command", "muted": False},
    }


class FrameTest(unittest.TestCase):
    def test_frame_matches_layout_geometry(self):
        # Renderer and tap-map share one frame: the GLANCE map fills
        # header..base (macbook_layout), or taps land where the map is
        # not. frame() with those folds must equal fit() on the same
        # area, or draw/tap/region have drifted.
        box = macbook_map.union([d for d in DISPLAYS])
        top, bottom = macbook_layout.header_bottom(), PANEL_H
        pad = macbook_map.MAP_PAD
        area_w = PANEL_W - 2 * pad
        area_h = bottom - pad - top
        old = macbook_map.fit(box, area_w, area_h)
        new = macbook_map.frame(box, PANEL_W, PANEL_H, top=top,
                                bottom=bottom)
        self.assertEqual((old[0], old[1], old[2] + top), new)

    def test_header_is_slimmer_than_old_bands(self):
        # The inefficiency the captain complained about: the merged
        # header must stay well under the old 210/250 fixed bands.
        self.assertLess(macbook_layout.header_bottom(), 210)

    def test_degenerate_inputs_scale_zero(self):
        self.assertEqual(macbook_map.frame(None, PANEL_W, PANEL_H)[0], 0.0)
        self.assertEqual(macbook_map.frame(
            macbook_map.union([]), PANEL_W, PANEL_H)[0], 0.0)


class UnprojectTest(unittest.TestCase):
    def test_inverse_of_project(self):
        scale, ox, oy = macbook_map.frame(
            macbook_map.union(DISPLAYS), PANEL_W, PANEL_H)
        for qx, qy in [(0, 0), (1728, 1117), (-355, -1080),
                       (464, -283), (1500.5, 900.25)]:
            px, py = macbook_map.project(qx, qy, scale, ox, oy)
            back = macbook_map.unproject(px, py, scale, ox, oy)
            self.assertAlmostEqual(back[0], qx, places=6)
            self.assertAlmostEqual(back[1], qy, places=6)

    def test_zero_scale_and_garbage_is_none(self):
        self.assertIsNone(macbook_map.unproject(1, 2, 0, 0, 0))
        self.assertIsNone(macbook_map.unproject("a", 2, 1, 0, 0))
        self.assertIsNone(macbook_map.unproject(1, 2, "s", 0, 0))


class LocateTest(unittest.TestCase):
    def test_round_trip_both_displays(self):
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(box, PANEL_W, PANEL_H)
        cases = [((100, 100), 0), ((1500, 900), 0),
                 ((464, -283), 1), ((1000, -500), 1)]
        for (qx, qy), want_display in cases:
            px, py = macbook_map.project(qx, qy, scale, ox, oy)
            hit = macbook_map.locate(px, py, DISPLAYS, PANEL_W, PANEL_H)
            self.assertIsNotNone(hit, (qx, qy))
            self.assertEqual(hit["display_index"], want_display)
            self.assertAlmostEqual(hit["x"], qx, places=6)
            self.assertAlmostEqual(hit["y"], qy, places=6)

    def test_misses_are_none(self):
        # Header strip, letterbox padding, and off-panel points.
        for px, py in [(960, 100), (10, 260), (1910, 1070),
                       (-5, 500), (960, 5000)]:
            self.assertIsNone(
                macbook_map.locate(px, py, DISPLAYS, PANEL_W, PANEL_H),
                (px, py))

    def test_garbage_never_raises(self):
        for displays in (None, [], {}, [{"bounds": {"x": 1}}],
                         [{"bounds": {"x": 0, "y": 0, "w": 0, "h": 10}}],
                         "nope"):
            self.assertIsNone(
                macbook_map.locate(960, 600, displays, PANEL_W, PANEL_H))
        self.assertIsNone(macbook_map.locate("x", 600, DISPLAYS,
                                             PANEL_W, PANEL_H))
        self.assertIsNone(macbook_map.locate(960, 600, DISPLAYS,
                                             "w", PANEL_H))


class DaemonMouseTest(unittest.TestCase):
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

    def _show_macbook(self):
        self.daemon.show("macbook", {})
        self.daemon.feed("macbook", "state", feed_payload())

    def _panel_of(self, qx, qy):
        # The GLANCE map fills header..base: project through the same
        # frame the daemon tap-maps with, or taps drift.
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(
            box, PANEL_W, PANEL_H,
            top=macbook_layout.header_bottom(), bottom=PANEL_H)
        px, py = macbook_map.project(qx, qy, scale, ox, oy)
        return int(round(px)), int(round(py))

    def test_valid_tap_queues_quartz_command(self):
        self._show_macbook()
        px, py = self._panel_of(100, 100)
        result = self.daemon.request_mouse_move(px, py)
        self.assertTrue(result["ok"], result)
        cmd = result["command"]
        self.assertEqual(cmd["display_index"], 0)
        self.assertAlmostEqual(cmd["x"], 100, delta=5)
        self.assertAlmostEqual(cmd["y"], 100, delta=5)
        self.assertIn("id", cmd)
        # Poller fetch path: visible until acted on, then gone.
        self.assertEqual(self.daemon.take_mouse_move()["id"], cmd["id"])
        self.assertIsNone(self.daemon.take_mouse_move(since=cmd["ts"]))
        self.assertIsNotNone(self.daemon.take_mouse_move(since=0))

    def test_second_display_tap(self):
        self._show_macbook()
        px, py = self._panel_of(464, -283)
        result = self.daemon.request_mouse_move(px, py)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["command"]["display_index"], 1)
        self.assertAlmostEqual(result["command"]["x"], 464, delta=5)

    def test_wrong_view_refused(self):
        self.daemon.show("clock", {})
        self.daemon.feed("macbook", "state", feed_payload())
        result = self.daemon.request_mouse_move(960, 600)
        self.assertFalse(result["ok"])
        self.assertIn("not showing", result["reason"])
        self.assertIsNone(self.daemon.take_mouse_move())

    def test_stale_or_missing_feed_refused(self):
        self._show_macbook()
        px, py = self._panel_of(100, 100)
        self.daemon.feed("macbook", "state",
                         feed_payload(ts=time.time() - 100))
        result = self.daemon.request_mouse_move(px, py)
        self.assertFalse(result["ok"])
        self.assertIn("feed", result["reason"])

    def test_tap_outside_map_refused(self):
        self._show_macbook()
        result = self.daemon.request_mouse_move(960, 100)  # header
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["reason"])
        self.assertIsNone(self.daemon.take_mouse_move())

    def test_aim_mode_refused(self):
        # Positioning belongs to GLANCE: in AIM the same pixel is a
        # review tap, never a warp -- refuse, never mis-move.
        self._show_macbook()
        px, py = self._panel_of(100, 100)
        self.daemon.show("macbook", {"mode": "aim"})
        result = self.daemon.request_mouse_move(px, py)
        self.assertFalse(result["ok"])
        self.assertIn("GLANCE", result["reason"])
        self.assertIsNone(self.daemon.take_mouse_move())

    def test_malformed_raises(self):
        self._show_macbook()
        for px, py in [("834", 536), (834.0, 536), (True, 536),
                       (None, 536), (-1, 536), (1920, 536),
                       (834, 1080), (99999, 99999), (834, None)]:
            with self.assertRaises(ValueError, msg=repr((px, py))):
                self.daemon.request_mouse_move(px, py)
        self.assertIsNone(self.daemon.take_mouse_move())

    def test_ttl_expires_stale_command(self):
        self._show_macbook()
        px, py = self._panel_of(100, 100)
        cmd = self.daemon.request_mouse_move(px, py)["command"]
        self.daemon.mouse_pending["ts"] -= 60
        self.assertIsNone(self.daemon.take_mouse_move())
        self.assertIsNone(self.daemon.take_mouse_move(since=0))

    def test_bad_since_is_zero(self):
        self._show_macbook()
        px, py = self._panel_of(100, 100)
        cmd = self.daemon.request_mouse_move(px, py)["command"]
        self.assertEqual(self.daemon.take_mouse_move(since="junk")["id"],
                         cmd["id"])


class PollerFetchTest(unittest.TestCase):
    def _fetch(self, doc):
        import urllib.request

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return json.dumps(doc).encode()

        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            return macos_state.fetch_mouse_command("http://x:9", since=5.0)
        finally:
            urllib.request.urlopen = real

    def test_command_parsed(self):
        cmd = self._fetch({"command": {"x": 100.0, "y": -283.5,
                                       "display_index": 1, "ts": 9.0}})
        self.assertEqual((cmd["x"], cmd["y"], cmd["ts"]), (100.0, -283.5, 9.0))

    def test_nothing_pending_is_none(self):
        self.assertIsNone(self._fetch({"command": None}))
        self.assertIsNone(self._fetch({}))
        self.assertIsNone(self._fetch({"command": {"x": "bogus"}}))

    def test_transport_failure_is_none(self):
        import urllib.request
        real = urllib.request.urlopen

        def boom(*a, **k):
            raise ConnectionError("down")

        urllib.request.urlopen = boom
        try:
            self.assertIsNone(
                macos_state.fetch_mouse_command("http://x:9", since=0))
        finally:
            urllib.request.urlopen = real

    @unittest.skipUnless(sys.platform == "darwin", "needs Quartz")
    def test_direct_warp_round_trips(self):
        import Quartz
        before = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        try:
            macos_state.warp_mouse(50, 50)
            at = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            self.assertAlmostEqual(at.x, 50, delta=2)
            self.assertAlmostEqual(at.y, 50, delta=2)
        finally:
            Quartz.CGWarpMouseCursorPosition((before.x, before.y))


if __name__ == "__main__":
    unittest.main()
