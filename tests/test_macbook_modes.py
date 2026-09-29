"""Merged macbook feature tests: GLANCE/AIM modes + tab step.

One feature, two full-canvas modes: the header tab strip (step/window/
hit geometry), the daemon mode/tab slots (closed touch actions with
static bodies), the retired-view feed compat, and the degraded states in
BOTH modes. Coordinate-only tap contract pins included.

Run from the repo root:  python3 -m unittest tests.test_macbook_modes -v
"""

import base64
import inspect
import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
import touch
from touch import action_request, resolve_action

import macbook as macbook_renderer
import macbook_layout as lay

PANEL_W, PANEL_H = 1920, 1080
DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True}]


def state_payload(ts=None):
    return {
        "ts": ts if ts is not None else time.time(),
        "accessibility_trusted": True,
        "focus": {"app_name": "WezTerm",
                  "window_title": "macbookpro: fm-",
                  "window_bounds": {"x": 0, "y": 0,
                                    "w": 1728, "h": 1117},
                  "display_index": 0},
        "mouse": {"x": 464, "y": 283, "display_index": 0},
        "displays": [{"bounds": dict(d["bounds"]), "main": d["main"]}
                     for d in DISPLAYS],
        "talon": {"mode": "command", "muted": False},
    }


def apps_payload(ts=None, apps=("Safari", "Terminal", "Mail")):
    return {"ts": ts if ts is not None else time.time(),
            "apps": list(apps), "focused": "Safari"}


class FakeFramebuffer(displayd.Framebuffer):
    def __init__(self, w=1920, h=1080):
        self.width, self.height, self.bpp = w, h, 32
        self.stride = w * 4
        self.fd = None
        self.last_frame = None
        self.blanked = False
        self.saved_brightness = None
        self._brightness = 200
        self._max = 255
        self.backlight = "/fake/backlight0"
        self.vt_fd = None

    def present(self, img):
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        self.last_frame = img.convert("RGB").tobytes("raw", "BGRX")


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self._real_fb = displayd.Framebuffer
        displayd.Framebuffer = FakeFramebuffer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, displayd, "Framebuffer", self._real_fb)

    def make_daemon(self):
        return displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"))


class LayoutTest(unittest.TestCase):
    def test_header_slimmer_than_old_bands(self):
        self.assertLess(lay.header_bottom(), 210)
        self.assertLess(lay.header_bottom(), 250)

    def test_step_wraps_and_zeros_when_empty(self):
        self.assertEqual(lay.step(0, 1, 3), 1)
        self.assertEqual(lay.step(2, 1, 3), 0)
        self.assertEqual(lay.step(0, -1, 3), 2)
        self.assertEqual(lay.step(0, 1, 0), 0)
        self.assertEqual(lay.step(0, 1, None), 0)
        self.assertEqual(lay.step("junk", 1, 3), 1)

    def test_window_keeps_highlight_visible(self):
        self.assertEqual(lay.window_start(0, 3), 0)
        self.assertEqual(lay.window_start(2, 3), 0)
        start, slots = lay.visible_slots(7, 8)
        self.assertEqual(start, 2)
        self.assertEqual(slots, [2, 3, 4, 5, 6, 7])
        self.assertEqual(lay.visible_slots(0, 3)[1], [0, 1, 2])

    def test_touch_regions_generated_order(self):
        entries = lay.touch_regions(1920, 1080)
        ids = [e["id"] for e in entries]
        self.assertEqual(ids, ["mac-to-glance", "mac-to-aim",
                               "mac-tab-prev", "mac-tab-next",
                               "mac-focus", "mac-map", "mac-zoom"])
        # The click catcher is the whole panel and sits LAST, so header
        # controls win their pixels first (first match wins).
        self.assertEqual(entries[-1]["rect"], [0, 0, 1920, 1080])
        self.assertEqual(entries[-1]["action"]["name"], "macbook_click")
        actions = {e["id"]: e["action"] for e in entries}
        self.assertEqual(actions["mac-to-aim"],
                         {"name": "macbook_mode", "mode": "aim"})
        self.assertEqual(actions["mac-to-glance"],
                         {"name": "macbook_mode", "mode": "glance"})
        self.assertEqual(actions["mac-tab-prev"],
                         {"name": "talon_tab", "dir": -1})
        self.assertEqual(actions["mac-tab-next"],
                         {"name": "talon_tab", "dir": 1})
        # Header controls tile the strip without overlap.
        focus = entries[4]["rect"]
        self.assertEqual(focus[1] + focus[3],
                         lay.header_bottom() - 8)
        for e in entries[1:5]:
            self.assertLessEqual(e["rect"][1] + e["rect"][3],
                                 lay.header_bottom())

    def test_header_controls_clear_composited_badges(self):
        # The home badge (top-left 160x160) and sleep badge (top-right
        # 160x160) overlay every view: hidden controls would still win
        # taps (scoped regions precede global ones) and steal badge taps.
        entries = {e["id"]: e["rect"]
                   for e in lay.touch_regions(1920, 1080)}
        focus = entries["mac-focus"]
        self.assertGreaterEqual(focus[0], 160)
        prev = entries["mac-tab-prev"]
        self.assertGreaterEqual(prev[0], 160)
        aim = entries["mac-to-aim"]
        self.assertLessEqual(aim[0] + aim[2], 1760)
        back = entries["mac-to-glance"]
        self.assertGreaterEqual(back[1], 900)

    def test_chip_hit_never_raises(self):
        apps = ["A", "B"]
        self.assertIsNone(lay.chip_hit("x", 1, 1920, apps, 0))
        self.assertIsNone(lay.chip_hit(1, 1, "w", apps, 0))
        self.assertIsNone(lay.chip_hit(1, 1, 1920, "junk", 0))
        self.assertIsNone(lay.chip_hit(1, 1, 1920, apps, "junk"))


class ParamsTest(unittest.TestCase):
    def test_coerce_mode_defaults_glance(self):
        self.assertEqual(macbook_renderer.coerce_mode({}), "glance")
        self.assertEqual(macbook_renderer.coerce_mode(None), "glance")
        self.assertEqual(macbook_renderer.coerce_mode({"mode": "aim"}),
                         "aim")
        self.assertEqual(macbook_renderer.coerce_mode({"mode": "AIM"}),
                         "aim")
        self.assertEqual(macbook_renderer.coerce_mode({"mode": "zoom"}),
                         "glance")

    def test_coerce_tab_bounds_below(self):
        self.assertEqual(macbook_renderer.coerce_tab({}), 0)
        self.assertEqual(macbook_renderer.coerce_tab({"tab": 4}), 4)
        self.assertEqual(macbook_renderer.coerce_tab({"tab": -2}), 0)
        self.assertEqual(macbook_renderer.coerce_tab({"tab": "junk"}), 0)

    def test_params_advertise_mode_and_tab(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        params = found["macbook"]["params"]
        self.assertIn("mode", params)
        self.assertIn("tab", params)
        self.assertIn("state", found["macbook"]["inputs"])
        self.assertIn("zoom", found["macbook"]["inputs"])


class ModeSlotTest(DaemonCase):
    def _ready(self):
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        daemon.feed("macbook", "state", state_payload())
        daemon.feed("talon_apps", "state", apps_payload())
        return daemon

    def test_mode_switch_preserves_tab(self):
        daemon = self._ready()
        daemon.request_tab_step(1)
        result = daemon.request_mode_move("aim")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "aim")
        self.assertEqual(result["tab"], 1)
        self.assertEqual(daemon.current_params["mode"], "aim")
        self.assertEqual(daemon.current_params["tab"], 1)
        back = daemon.request_mode_move("glance")
        self.assertTrue(back["ok"])
        self.assertEqual(back["tab"], 1)

    def test_bad_mode_raises(self):
        daemon = self._ready()
        with self.assertRaises(ValueError):
            daemon.request_mode_move("zoom")
        with self.assertRaises(ValueError):
            daemon.request_mode_move(None)

    def test_mode_refused_unless_showing(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        result = daemon.request_mode_move("aim")
        self.assertFalse(result["ok"])
        self.assertIn("not showing", result["reason"])

    def test_tab_steps_and_wraps(self):
        daemon = self._ready()
        self.assertEqual(daemon.request_tab_step(1)["tab"], 1)
        self.assertEqual(daemon.request_tab_step(1)["tab"], 2)
        self.assertEqual(daemon.request_tab_step(1)["tab"], 0)
        self.assertEqual(daemon.request_tab_step(-1)["tab"], 2)
        self.assertEqual(daemon._macbook_mode(), "glance")

    def test_tab_bad_direction_raises(self):
        daemon = self._ready()
        for bad in (0, 2, -2, "x", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                daemon.request_tab_step(bad)

    def test_tab_refused_in_aim(self):
        daemon = self._ready()
        daemon.show("macbook", {"mode": "aim"})
        result = daemon.request_tab_step(1)
        self.assertFalse(result["ok"])
        self.assertIn("GLANCE", result["reason"])

    def test_tab_refused_without_apps(self):
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        daemon.feed("macbook", "state", state_payload())
        result = daemon.request_tab_step(1)
        self.assertFalse(result["ok"])
        self.assertIn("fresh", result["reason"])

    def test_http_routes(self):
        post_src = inspect.getsource(displayd.Handler.do_POST)
        self.assertIn('"/macbook/mode"', post_src)
        self.assertIn('"/talon/tab"', post_src)


class ModeTabActionTest(unittest.TestCase):
    def test_table_entries_are_closed(self):
        mode = touch.ACTION_TABLE["macbook_mode"]
        self.assertEqual((mode["method"], mode["path"]),
                         ("POST", "/macbook/mode"))
        tab = touch.ACTION_TABLE["talon_tab"]
        self.assertEqual((tab["method"], tab["path"]),
                         ("POST", "/talon/tab"))

    def test_dispatch_static_bodies(self):
        self.assertEqual(
            action_request({"name": "macbook_mode", "mode": "aim"}),
            ("POST", "/macbook/mode", {"mode": "aim"}))
        self.assertEqual(
            resolve_action({"name": "talon_tab", "dir": -1}),
            ("POST", "/talon/tab", {"dir": -1}))

    def test_malformed_refused(self):
        for bad in ({"name": "macbook_mode"},
                    {"name": "macbook_mode", "mode": "zoom"},
                    {"name": "talon_tab"},
                    {"name": "talon_tab", "dir": 0},
                    {"name": "talon_tab", "dir": 2},
                    {"name": "talon_tab", "dir": "1"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                action_request(bad)


class AimContractTest(unittest.TestCase):
    def test_aim_draw_takes_no_state(self):
        # Pin the captain's reasoning in code: the AIM surface cannot
        # see the mouse position -- it is not even an argument.
        import macbook_aim
        params = inspect.signature(macbook_aim.draw).parameters
        self.assertEqual(list(params),
                         ["img", "draw", "screen", "zoom", "stale",
                          "font"])

    def test_aim_caption_carries_no_coordinates(self):
        # The caption proves freshness (scale + age); the point stays
        # out of the picture by design.
        import macbook_aim
        src = inspect.getsource(macbook_aim.draw)
        self.assertNotIn('zoom.get("x")', src)
        self.assertNotIn("mouse", src)


class DrawSmokeTest(unittest.TestCase):
    class Scr:
        W, H = PANEL_W, PANEL_H

        def new_image(self, bg):
            from PIL import Image
            return Image.new("RGB", (self.W, self.H), bg)

        def color(self, value, default):
            return default

        def font_path(self, name):
            return None

    def _zoom(self, color=(40, 90, 140)):
        from PIL import Image
        shot = Image.new("RGB", (480, 360), color)
        buf = io.BytesIO()
        shot.save(buf, "JPEG")
        return {"ts": time.time(), "x": 464.0, "y": 283.0,
                "jpeg": base64.b64encode(buf.getvalue()).decode()}

    def test_glance_draws_full_canvas(self):
        img = macbook_renderer._draw(
            self.Scr(), "MACBOOK", "glance", 0, state_payload(), False,
            ["Safari", "Terminal", "Mail"], False, (10, 10, 14), None)
        self.assertEqual(img.size, (PANEL_W, PANEL_H))

    def test_aim_draws_full_canvas(self):
        img = macbook_renderer._draw(
            self.Scr(), "MACBOOK", "aim", 0, state_payload(), False,
            ["Safari"], False, (10, 10, 14), self._zoom())
        self.assertEqual(img.size, (PANEL_W, PANEL_H))

    def test_waiting_both_modes(self):
        for mode in ("glance", "aim"):
            img = macbook_renderer._draw(
                self.Scr(), "MACBOOK", mode, 0, None, False, [], False,
                (10, 10, 14), None)
            self.assertEqual(img.size, (PANEL_W, PANEL_H))

    def test_stale_and_app_only_glance(self):
        stale_state = state_payload(ts=time.time() - 100)
        img = macbook_renderer._draw(
            self.Scr(), "MACBOOK", "glance", 0, stale_state, True,
            ["Safari"], True, (10, 10, 14), None)
        self.assertEqual(img.size, (PANEL_W, PANEL_H))
        app_only = state_payload()
        app_only["focus"] = {"app_name": "NoWindows"}
        app_only["accessibility_trusted"] = False
        img = macbook_renderer._draw(
            self.Scr(), "MACBOOK", "glance", 0, app_only, False,
            [], False, (10, 10, 14), None)
        self.assertEqual(img.size, (PANEL_W, PANEL_H))

    def test_stale_aim_keeps_image_with_tag(self):
        stale_state = state_payload(ts=time.time() - 100)
        img = macbook_renderer._draw(
            self.Scr(), "MACBOOK", "aim", 0, stale_state, True,
            [], False, (10, 10, 14), self._zoom())
        self.assertEqual(img.size, (PANEL_W, PANEL_H))

    def test_key_separates_modes(self):
        apps = apps_payload()
        zoom = self._zoom()
        state = state_payload()
        glance = macbook_renderer._key(state, apps, zoom, "glance", 0)
        aim = macbook_renderer._key(state, apps, zoom, "aim", 0)
        self.assertNotEqual(glance, aim)
        tabbed = macbook_renderer._key(state, apps, zoom, "glance", 1)
        self.assertNotEqual(glance, tabbed)


if __name__ == "__main__":
    unittest.main()
