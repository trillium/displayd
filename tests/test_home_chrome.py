"""Home-chrome tests: overlay chain, badge draw, touch region, daemon wiring.

No framebuffer needed: DisplayDaemon is constructed against the same
FakeFramebuffer pattern as test_playlist (real PIL frames in memory,
fake wires), so overlay assertions run on this host.

Run from the repo root:  python3 -m unittest tests.test_home_chrome -v
"""

import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
import home_chrome
from PIL import Image


class FakeFramebuffer(displayd.Framebuffer):
    """Real PIL frames, fake wires (mirrors test_playlist)."""

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


def top_left_differs(img, side=160):
    """True when any pixel in the top-left square is non-black."""
    px = img.load()
    for y in range(side):
        for x in range(side):
            if px[x, y] != (0, 0, 0):
                return True
    return False


class ChainTest(unittest.TestCase):
    def test_applies_in_order(self):
        calls = []

        def first(img):
            calls.append("first")
            return img

        def second(img):
            calls.append("second")
            return img

        out = home_chrome.chain_overlays(first, second)(black(64, 64))
        self.assertEqual(calls, ["first", "second"])
        self.assertIsNotNone(out)

    def test_none_entries_skipped(self):
        out = home_chrome.chain_overlays(None, None)(black(64, 64))
        self.assertIsNotNone(out)

    def test_failing_layer_never_blanks(self):
        def bad(img):
            raise RuntimeError("SYNTHETIC chrome failure")

        def good(img):
            img.putpixel((5, 5), (255, 255, 255))
            return img

        out = home_chrome.chain_overlays(bad, good)(black(64, 64))
        self.assertEqual(out.getpixel((5, 5)), (255, 255, 255))

    def test_none_return_keeps_frame(self):
        def nothing(img):
            return None

        img = black(64, 64)
        self.assertIs(home_chrome.chain_overlays(nothing)(img), img)


class BadgeTest(unittest.TestCase):
    def test_badge_draws_top_left(self):
        img = home_chrome.draw_home_button(black())
        self.assertTrue(top_left_differs(img))

    def test_badge_reads_on_light_background(self):
        img = home_chrome.draw_home_button(
            Image.new("RGB", (1920, 1080), (255, 255, 255)))
        self.assertTrue(top_left_differs(img, side=160))

    def test_badge_never_raises(self):
        img = home_chrome.draw_home_button(None, rect="bogus")
        self.assertIsNone(img)

    def test_home_rect_inside_left_strip(self):
        rect = home_chrome.home_rect(1920, 1080)
        self.assertEqual(rect, [0, 0, 160, 160])
        small = home_chrome.home_rect(160, 90)
        self.assertEqual(small[0:2], [0, 0])
        self.assertTrue(small[2] <= 160 and small[3] <= 160)


class RegionTest(unittest.TestCase):
    def test_home_region_reuses_select_view(self):
        region = home_chrome.home_region()
        self.assertEqual(region["id"], "home")
        self.assertEqual(region["rect"], [0, 0, 160, 160])
        self.assertEqual(region["action"],
                         {"name": "select_view", "view": "picker"})

    def test_home_region_wins_overlap_by_order(self):
        import touch
        regions = [home_chrome.home_region(),
                   {"id": "screen-on", "rect": [0, 0, 160, 1080]}]
        self.assertEqual(touch.hit_test(10, 10, regions), "home")
        self.assertEqual(touch.hit_test(10, 500, regions), "screen-on")

    def test_home_region_misses_picker_tiles(self):
        import touch
        from picker import picker_regions
        regions = ([home_chrome.home_region()] +
                   picker_regions(1920, 1080) +
                   [{"id": "screen-on", "rect": [0, 0, 160, 1080]}])
        # A tap in the first tile's middle still selects that tile.
        tiles = picker_regions(1920, 1080)
        x, y, w, h = tiles[0]["rect"]
        self.assertEqual(touch.hit_test(x + w // 2, y + h // 2, regions),
                         "view-clock")
        self.assertEqual(touch.hit_test(10, 10, regions), "home")


class SuppressionTest(unittest.TestCase):
    def test_suppressed_views_stay_clean(self):
        screen = types.SimpleNamespace(current_view="clock")
        overlay = home_chrome.home_overlay(screen)
        self.assertTrue(top_left_differs(overlay(black()), side=160))
        for view in ("picker", "reload", "notice"):
            screen.current_view = view
            self.assertFalse(top_left_differs(overlay(black()), side=160),
                             view)

    def test_overlay_never_raises(self):
        overlay = home_chrome.home_overlay(None)
        self.assertIsNotNone(overlay(black(64, 64)))


class DaemonWiringTest(unittest.TestCase):
    def setUp(self):
        self._real_fb = displayd.Framebuffer
        displayd.Framebuffer = FakeFramebuffer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        displayd.Framebuffer = self._real_fb

    def test_chain_draws_home_on_plain_view(self):
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"))
        self.addCleanup(daemon.playlist.stop)
        self.addCleanup(daemon.stop_watchdog)
        daemon.screen.current_view = "clock"
        out = daemon.screen.overlay(black())
        self.assertTrue(top_left_differs(out))

    def test_chain_suppresses_home_on_picker(self):
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"))
        self.addCleanup(daemon.playlist.stop)
        self.addCleanup(daemon.stop_watchdog)
        daemon.screen.current_view = "picker"
        out = daemon.screen.overlay(black())
        self.assertFalse(top_left_differs(out))

    def test_playlist_layer_survives_chain(self):
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"))
        self.addCleanup(daemon.playlist.stop)
        self.addCleanup(daemon.stop_watchdog)
        mod = types.ModuleType("synthetic_red")
        mod.NAME = "red"
        mod.DESCRIPTION = "SYNTHETIC"
        mod.STATIC = True
        mod.PARAMS = {}

        def run(screen, params, stop):
            screen.present(screen.new_image((200, 30, 30)))
        mod.run = run
        daemon.renderers["red"] = {"module": mod, "description": "",
                                    "params": {}, "inputs": {},
                                    "static": True}
        daemon.set_policy({"playlist": {
            "enabled": True, "tick_seconds": 0.05,
            "views": [{"renderer": "red", "dwell": 60}]}})
        deadline = time.time() + 5
        while daemon.playlist.progress() is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(daemon.playlist.progress(),
                             "playlist never started dwelling")
        # Bar fills the bottom edge AND the home badge draws -- both
        # layers in one composed frame.
        daemon.screen.current_view = "red"
        out = daemon.screen.overlay(black())
        px = out.load()
        bottom_lit = any(px[x, 1079] != (0, 0, 0)
                         for x in range(0, 1920, 7))
        self.assertTrue(bottom_lit, "playlist bar missing from chain")
        self.assertTrue(top_left_differs(out), "home badge missing")


if __name__ == "__main__":
    unittest.main()
