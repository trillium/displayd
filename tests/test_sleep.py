"""Sleep/wake tests: moon badge chrome, sleep view, and the power-off
return path (sleep on power-off, restore the pre-sleep view on wake).

No framebuffer needed: DisplayDaemon is constructed against the same
FakeFramebuffer pattern as test_home_chrome (real PIL frames in memory,
fake wires), and touch taps drive a fake client like test_touch.

Run from the repo root:  python3 -m unittest tests.test_sleep -v
"""

import os
import sys
import tempfile
import time
import types
import unittest

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
from ui import system_buttons as buttons
import touch
from touch import TouchService, TouchEvent, default_config


class FakeFramebuffer(displayd.Framebuffer):
    """Real PIL frames, fake wires (mirrors test_home_chrome)."""

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
        self.last_frame = img.convert("RGB")

    def raw(self, data):
        self.last_frame = data

    def repaint(self):
        pass

    def _read_int(self, path):
        if path.endswith("max_brightness"):
            return self._max
        if path.endswith("brightness"):
            return self._brightness
        if path.endswith("blank"):
            return 4 if self.blanked else 0
        return None

    def set_brightness(self, value):
        self._brightness = max(0, min(int(value), self._max))
        return True

    def set_blank(self, value):
        self.blanked = (int(value) != 0)
        return True

    def get_blank(self):
        return 4 if self.blanked else 0

    def take_console(self):
        return True


def black(w=1920, h=1080):
    return Image.new("RGB", (w, h), (0, 0, 0))


def top_right_differs(img, side=160):
    """True when any pixel in the top-right square is non-black."""
    w, _ = img.size
    px = img.load()
    for y in range(side):
        for x in range(w - side, w):
            if px[x, y] != (0, 0, 0):
                return True
    return False


class ChromeTest(unittest.TestCase):
    def test_badge_draws_top_right(self):
        img = buttons.draw_sleep_button(black())
        self.assertTrue(top_right_differs(img))

    def test_badge_reads_on_light_background(self):
        img = buttons.draw_sleep_button(
            Image.new("RGB", (1920, 1080), (255, 255, 255)))
        self.assertTrue(top_right_differs(img))

    def test_badge_never_raises(self):
        img = buttons.draw_sleep_button(None, rect="bogus")
        self.assertIsNone(img)

    def test_rect_inside_right_strip(self):
        self.assertEqual(buttons.sleep_rect(1920, 1080),
                         [1760, 0, 160, 160])
        small = buttons.sleep_rect(160, 90)
        self.assertTrue(small[0] + small[2] <= 160)
        self.assertTrue(small[2] <= 160 and small[3] <= 160)

    def test_region_reuses_screen_off(self):
        region = buttons.sleep_region()
        self.assertEqual(region["id"], "screen-off")
        self.assertEqual(region["rect"], [1760, 0, 160, 160])
        self.assertEqual(region["action"], {"name": "screen_off"})

    def test_region_wins_overlap_by_order(self):
        regions = [buttons.sleep_region(),
                   {"id": "playlist-next",
                    "rect": [1760, 0, 160, 1080]}]
        self.assertEqual(touch.hit_test(1800, 10, regions), "screen-off")
        self.assertEqual(touch.hit_test(1800, 500, regions),
                         "playlist-next")

    def test_overlay_draws_on_content_views(self):
        screen = types.SimpleNamespace(current_view="clock")
        overlay = buttons.system_overlay(screen, None, which=("sleep",))
        self.assertTrue(top_right_differs(overlay(black())))
        screen.current_view = "picker"
        self.assertTrue(top_right_differs(overlay(black())), "picker")
        screen.current_view = "unified"
        self.assertTrue(top_right_differs(overlay(black())), "unified")

    def test_overlay_suppressed_where_meaningless(self):
        screen = types.SimpleNamespace(current_view="clock")
        overlay = buttons.system_overlay(screen, None, which=("sleep",))
        for view in ("sleep", "reload", "notice"):
            screen.current_view = view
            self.assertFalse(top_right_differs(overlay(black())), view)

    def test_audit_exact_matches_drawn(self):
        entry = buttons.audit_exact("clock", 1920, 1080, which=("sleep",))
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["id"], "screen-off")
        self.assertEqual(entry[0]["rect"], [1760, 0, 160, 160])
        for view in ("sleep", "reload", "notice"):
            self.assertEqual(buttons.audit_exact(view, which=("sleep",)), [])


class SleepViewTest(unittest.TestCase):
    def test_loads_with_valid_schema(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("sleep", found)
        entry = found["sleep"]
        self.assertIn("module", entry)
        displayd.validate_params({}, entry.get("params") or {})

    def test_run_draws_hint_without_raising(self):
        mod = displayd.load_renderers(displayd.RENDERER_DIR)["sleep"]
        screen = displayd.Screen(FakeFbSmall())
        mod["module"].run(screen, {}, None)
        self.assertEqual(len(screen.fb.frames), 1)

    def test_run_accepts_hint_param(self):
        mod = displayd.load_renderers(displayd.RENDERER_DIR)["sleep"]
        screen = displayd.Screen(FakeFbSmall())
        mod["module"].run(screen, {"hint": "demo"}, None)
        self.assertEqual(len(screen.fb.frames), 1)


class FakeFbSmall:
    def __init__(self, w=480, h=270):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


class PowerTest(unittest.TestCase):
    def setUp(self):
        self._real_fb = displayd.Framebuffer
        displayd.Framebuffer = FakeFramebuffer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, displayd, "Framebuffer", self._real_fb)

    def make_daemon(self, clock=None):
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            clock=clock)
        self.addCleanup(daemon.playlist.stop)
        self.addCleanup(daemon.stop_watchdog)
        return daemon

    def test_off_switches_to_sleep_and_on_restores(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        daemon.set_power("off")
        self.assertEqual(daemon.current, "sleep")
        self.assertTrue(daemon.fb.blanked)
        daemon.set_power("on")
        self.assertEqual(daemon.current, "clock")
        self.assertFalse(daemon.fb.blanked)

    def test_off_during_transient_restores_base(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        daemon.notify("hello", duration=60)
        self.assertEqual(daemon.current, "notice")
        daemon.set_power("off")
        self.assertEqual(daemon.current, "sleep")
        daemon.set_power("on")
        # The transient itself is never the return target.
        self.assertEqual(daemon.current, "clock")

    def test_manual_show_while_dark_voids_return(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        daemon.set_power("off")
        daemon.show("chat", {})
        daemon.set_power("on")
        self.assertEqual(daemon.current, "chat")

    def test_double_off_then_on_restores_once(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        daemon.set_power("off")
        daemon.set_power("off")
        self.assertEqual(daemon.current, "sleep")
        daemon.set_power("on")
        self.assertEqual(daemon.current, "clock")
        daemon.set_power("on")
        self.assertEqual(daemon.current, "clock")

    def test_on_without_sleep_is_plain_power_on(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        daemon.set_power("on")
        self.assertEqual(daemon.current, "clock")
        self.assertFalse(daemon.fb.blanked)

    def test_on_never_yanks_a_lit_view(self):
        daemon = self.make_daemon()
        daemon.show("chat", {})
        daemon.set_power("on")
        self.assertEqual(daemon.current, "chat")

    def test_wake_from_blank_falls_back_to_clock(self):
        daemon = self.make_daemon()
        daemon.clear()
        self.assertIsNone(daemon.current)
        daemon.set_power("off")
        self.assertEqual(daemon.current, "sleep")
        daemon.set_power("on")
        # No return pending: land navigable, never lit-sleep.
        self.assertEqual(daemon.current, "clock")
        self.assertFalse(daemon.fb.blanked)

    def test_manual_demo_wake_lands_clock(self):
        daemon = self.make_daemon()
        daemon.show("sleep", {})
        daemon.set_power("on")
        self.assertEqual(daemon.current, "clock")

    def test_off_holds_playlist(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        daemon.set_policy({"playlist": {
            "enabled": True, "tick_seconds": 0.5,
            "views": [{"renderer": "clock", "dwell": 60}]}})
        daemon.set_power("off")
        self.assertEqual(daemon.playlist.paused_by, "manual")
        daemon.set_power("on")

    def test_check_idle_sleeps_and_feed_wake_restores(self):
        now = [1000.0]
        daemon = self.make_daemon(clock=lambda: now[0])
        daemon.show("clock", {})
        daemon.set_policy({"idle": {"enabled": True,
                                    "after_seconds": 5}})
        now[0] += 6
        daemon.check_idle()
        self.assertTrue(daemon.fb.blanked)
        self.assertEqual(daemon.current, "sleep")
        # A feed while idle-dark lights up AND lands the return view:
        # without the restore the panel would sit lit on sleep.
        daemon.feed("chat", "message", {"author": "t", "text": "hi"})
        self.assertFalse(daemon.fb.blanked)
        self.assertEqual(daemon.current, "clock")

    def test_chain_draws_both_badges(self):
        daemon = self.make_daemon()
        daemon.screen.current_view = "clock"
        out = daemon.screen.overlay(black())
        px = out.load()
        left = any(px[x, y] != (0, 0, 0)
                   for y in range(160) for x in range(160))
        right = any(px[x, y] != (0, 0, 0)
                    for y in range(160) for x in range(1760, 1920))
        self.assertTrue(left, "home badge missing from chain")
        self.assertTrue(right, "sleep badge missing from chain")

    def test_chain_draws_neither_on_sleep(self):
        daemon = self.make_daemon()
        daemon.screen.current_view = "sleep"
        out = daemon.screen.overlay(black())
        px = out.load()
        corners = (
            [px[x, y] for y in range(160) for x in range(160)] +
            [px[x, y] for y in range(160) for x in range(1760, 1920)])
        self.assertTrue(all(p == (0, 0, 0) for p in corners))


def sleep_touch_config(view):
    """Host-shaped config: badge globals + fullscreen sleep wake scope."""
    cfg = default_config()
    cfg.update({"width": 1920, "height": 1080,
                "calibration": {"x_min": 0, "x_max": 1920,
                                "y_min": 0, "y_max": 1080,
                                "swap_xy": False, "invert_x": False,
                                "invert_y": False, "rotation": 0},
                "tap_max_seconds": 60, "debounce_seconds": 0,
                "tap_options": {"enabled": False},
                "regions": [
                    {"id": "home", "rect": [0, 0, 160, 160],
                     "action": {"name": "select_view",
                               "view": "picker"}},
                    {"id": "screen-off", "rect": [1760, 0, 160, 160],
                     "action": {"name": "screen_off"}},
                    {"id": "screen-on", "rect": [0, 0, 160, 1080],
                     "action": {"name": "screen_on"}},
                    {"id": "playlist-next", "rect": [1760, 0, 160, 1080],
                     "action": {"name": "playlist_next"}},
                ],
                "view_regions": {
                    "sleep": [
                        {"id": "wake", "rect": [0, 0, 1920, 1080],
                         "action": {"name": "screen_on"}},
                    ],
                }})
    return cfg


class FakeSleepClient:
    """Dispatch recorder fixed on one /state view (like a real tap)."""

    def __init__(self, view):
        self.view = view
        self.dispatched = []

    def state(self, timeout=None):
        return {"renderer": self.view}

    def tap_dismiss(self, dry_run=False):
        return {"action": "tap_dismiss", "dry_run": bool(dry_run),
                "response": {"dismissed": False}}

    def dispatch(self, action, dry_run=False, panel=None):
        self.dispatched.append(action)
        return {"action": action["name"], "dry_run": bool(dry_run)}


class TouchWiringTest(unittest.TestCase):
    def _tap(self, svc, x, y):
        svc.handle_frame([TouchEvent("down", 0, x, y)], dry_run=True)
        return svc.handle_frame([TouchEvent("up", 0, x, y)], dry_run=True)

    def test_badge_tap_sleeps_while_awake(self):
        client = FakeSleepClient("clock")
        svc = TouchService(sleep_touch_config("clock"), client=client)
        self._tap(svc, 1840, 80)
        self.assertEqual([a["name"] for a in client.dispatched],
                         ["screen_off"])

    def test_any_tap_wakes_while_asleep(self):
        for x, y in ((960, 540), (80, 80), (1840, 80), (10, 1070)):
            client = FakeSleepClient("sleep")
            svc = TouchService(sleep_touch_config("sleep"), client=client)
            self._tap(svc, x, y)
            self.assertEqual([a["name"] for a in client.dispatched],
                             ["screen_on"], (x, y))

    def test_wake_shadows_badge_while_asleep(self):
        client = FakeSleepClient("sleep")
        svc = TouchService(sleep_touch_config("sleep"), client=client)
        cands = svc.candidate_regions("sleep")
        self.assertEqual(cands[0]["id"], "wake")
        self.assertEqual(touch.hit_test(1840, 80, cands), "wake")

    def test_badge_wins_strip_while_awake(self):
        client = FakeSleepClient("clock")
        svc = TouchService(sleep_touch_config("clock"), client=client)
        cands = svc.candidate_regions("clock")
        self.assertEqual(touch.hit_test(1840, 80, cands), "screen-off")
        self.assertEqual(touch.hit_test(1840, 500, cands),
                         "playlist-next")

    def test_example_configs_validate(self):
        root = os.path.join(os.path.dirname(__file__), os.pardir)
        for name in ("touch.json.example", "touch-home.json.example"):
            cfg = touch.load_config(os.path.join(root, name))
            ids = [r["id"] for r in cfg["regions"]]
            self.assertIn("screen-off", ids)
            # Badge precedes the strip it overlaps.
            self.assertLess(ids.index("screen-off"),
                            ids.index("playlist-next"))
            scoped = cfg["view_regions"]["sleep"]
            self.assertEqual(len(scoped), 1)
            self.assertEqual(scoped[0]["rect"], [0, 0, 1920, 1080])
            self.assertEqual(scoped[0]["action"], {"name": "screen_on"})


if __name__ == "__main__":
    unittest.main()
