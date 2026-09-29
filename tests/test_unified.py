"""Tests for the unified home screen (picker tiles + live apps dock).

Run from the repo root:  python3 -m pytest tests/test_unified.py -v
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from PIL import Image, ImageChops

import touch_audit
from renderers import home_chrome
from renderers import unified as un
from renderers import unified_dock as dock

W, H = 1920, 1080
VIEWS_19 = ["activity", "beads", "beads-detail", "chat", "clock",
            "feed_health", "life", "macbook", "options", "picker",
            "resources", "retro_grid", "row", "services", "sleep",
            "solid", "stream", "talon_apps", "touch_confidence"]
LEFT_11 = ["Calendar", "Find My", "Mail", "Maps", "Music", "Notes",
           "Photos", "Safari", "Slack", "Talon", "Terminal"]
RIGHT_9 = ["Activity Monitor", "Code", "Discord", "Finder", "Firebot v5",
           "Google Chrome", "Google Chrome Beta", "interceptor-bridge",
           "LuLu"]
DISPLAYS = [{"bounds": {"x": 0, "y": 0}, "main": True},
            {"bounds": {"x": 1920, "y": 0}}]


def live_state(ts=None):
    windows = {a: {"d": 0} for a in LEFT_11}
    windows.update({a: {"d": 1} for a in RIGHT_9})
    return {"ts": time.time() if ts is None else ts,
            "apps": LEFT_11 + RIGHT_9, "focused": "Google Chrome",
            "windows": windows, "displays": DISPLAYS}


class FakeScreen:
    W, H = W, H

    def __init__(self, states=()):
        self.frames = []
        self._states = list(states)

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return default

    def font_path(self, family="DejaVuSans-Bold"):
        return None

    def get_input(self, renderer, input_name):
        if renderer == "talon_apps" and input_name == "state":
            return list(self._states)
        return []


def fake_renderers():
    table = {}
    for name in VIEWS_19:
        table[name] = {"module": object(), "params": {}}
    table["unified"] = {"module": object(), "params": {}}
    table["image"] = {"module": object(),
                      "params": {"path": {"type": "string",
                                          "required": True}}}
    return table


class DefaultsTest(unittest.TestCase):
    def test_grid_and_dock_defaults_at_panel_size(self):
        self.assertEqual(un.default_grid(W, H), [160, 40, 1600, 700])
        self.assertEqual(un.coerce_grid({}, W, H), [160, 40, 1600, 700])
        self.assertEqual(un.default_dock(W, H), [80, 770, 1760, 230])
        self.assertEqual(un.coerce_dock({}, W, H), [80, 770, 1760, 230])

    def test_garbage_rects_fall_back(self):
        self.assertEqual(un.coerce_grid({"rect": "nope"}, W, H),
                         [160, 40, 1600, 700])
        self.assertEqual(un.coerce_dock({"dock": [0, 0, -5, 10]}, W, H),
                         [80, 770, 1760, 230])

    def test_tile_views_explicit_wins(self):
        self.assertEqual(un.tile_views({"views": ["clock"]}), ["clock"])

    def test_tile_views_live_minus_self(self):
        self.assertEqual(un.live_tile_views(fake_renderers()), VIEWS_19)
        self.assertEqual(un.tile_views({}, fake_renderers()), VIEWS_19)

    def test_tile_views_fallback_without_table(self):
        from renderers import picker as pk
        self.assertEqual(un.tile_views({}), list(pk.DEFAULT_VIEWS))


class RegionsTest(unittest.TestCase):
    def test_sample_map_shape(self):
        regs = un.unified_regions(W, H, list(VIEWS_19))
        self.assertEqual(len(regs), 21)
        self.assertEqual(regs[0]["id"], "screen-off")
        self.assertEqual(regs[0]["rect"], [1760, 0, 160, 160])
        self.assertEqual(regs[0]["action"], {"name": "screen_off"})
        tiles = regs[1:-1]
        self.assertEqual(len(tiles), 19)
        for entry, view in zip(tiles, VIEWS_19):
            self.assertEqual(entry["id"], "uview-%s" % view)
            self.assertEqual(entry["action"],
                             {"name": "select_view", "view": view})
        self.assertEqual(regs[-1]["id"], "apps-dock")
        self.assertEqual(regs[-1]["rect"], [80, 770, 1760, 230])
        self.assertEqual(regs[-1]["action"],
                         {"name": "select_view", "view": "talon_apps"})
        self.assertFalse([r for r in regs if r["id"] == "home"])

    def test_tile_rects_match_picker_geometry(self):
        from renderers import picker as pk
        regs = un.unified_regions(W, H, list(VIEWS_19))
        expect = pk.grid_geometry([160, 40, 1600, 700], 19)
        for entry, rect in zip(regs[1:-1], expect):
            self.assertEqual(entry["rect"], list(rect))


class DockSummaryTest(unittest.TestCase):
    def test_real_content_summary(self):
        summ = dock.dock_summary(live_state())
        self.assertEqual(summ["mode"], "left=D1 right=other")
        self.assertEqual(summ["count"], 20)
        self.assertEqual(summ["focused"], "Google Chrome")
        self.assertEqual(summ["overflow"], 2)
        self.assertEqual((summ["left"], summ["right"]), (11, 9))

    def test_empty_and_garbage_never_raise(self):
        self.assertEqual(dock.dock_summary(None)["count"], 0)
        self.assertEqual(dock.dock_summary("nope")["mode"],
                         "one screen: split")


class FramesTest(unittest.TestCase):
    def _frame(self, state, stale):
        from renderers import picker as pk
        screen = FakeScreen()
        grid = un.coerce_grid({}, W, H)
        views = un.tile_views({"views": list(VIEWS_19)})
        return un.draw(screen, views, pk.grid_geometry(grid, len(views)),
                       grid, un.coerce_dock({}, W, H), state, stale,
                       (8, 10, 16), (255, 255, 255))

    def test_all_states_render_at_panel_size(self):
        for state, stale in ((live_state(), False), (None, False),
                             (live_state(ts=time.time() - 99), True)):
            frame = self._frame(state, stale)
            self.assertEqual(frame.size, (W, H))

    def test_tiles_untouched_across_dock_states(self):
        healthy = self._frame(live_state(), False)
        empty = self._frame(None, False)
        stale = self._frame(live_state(ts=time.time() - 99), True)
        grid_zone = (0, 0, W, 740)  # grid bottom; dock starts at y=770
        for other in (empty, stale):
            diff = ImageChops.difference(
                healthy.crop(grid_zone), other.crop(grid_zone))
            self.assertIsNone(diff.getbbox())

    def test_empty_and_stale_differ_in_dock_only(self):
        empty = self._frame(None, False)
        stale = self._frame(live_state(ts=time.time() - 99), True)
        dock_zone = (80, 770, 1840, 1000)
        diff = ImageChops.difference(empty.crop(dock_zone),
                                     stale.crop(dock_zone))
        self.assertIsNotNone(diff.getbbox())


class RunLoopTest(unittest.TestCase):
    def test_run_presents_live_frame(self):
        screen = FakeScreen([live_state()])
        stop = threading.Event()
        worker = threading.Thread(target=un.run,
                                  args=(screen, {}, stop))
        worker.start()
        deadline = time.time() + 5
        while not screen.frames and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        worker.join(timeout=5)
        self.assertTrue(screen.frames)
        self.assertEqual(screen.frames[-1].size, (W, H))

    def test_run_without_feed_still_presents(self):
        screen = FakeScreen()
        stop = threading.Event()
        worker = threading.Thread(target=un.run,
                                  args=(screen, {}, stop))
        worker.start()
        deadline = time.time() + 5
        while not screen.frames and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        worker.join(timeout=5)
        self.assertTrue(screen.frames)


class HomeWiringTest(unittest.TestCase):
    def test_unified_is_home(self):
        self.assertEqual(home_chrome.HOME_VIEW, "unified")
        self.assertIn("unified", home_chrome.SUPPRESSED_VIEWS)
        self.assertIn("picker", home_chrome.SUPPRESSED_VIEWS)
        self.assertNotIn("unified", ("sleep", "reload", "notice"))

    def test_audit_expects_tiles_plus_dock(self):
        expected = touch_audit.expected_for_view(
            "unified", {"views": list(VIEWS_19)}, W, H,
            picker_views=list(VIEWS_19))
        self.assertTrue(expected["checkable"])
        ids = [e["id"] for e in expected["exact"]]
        self.assertEqual(ids[:-1],
                         ["uview-%s" % v for v in VIEWS_19] + ["apps-dock"])
        self.assertEqual(ids[-1], "screen-off")
        dock_entry = [e for e in expected["exact"]
                      if e["id"] == "apps-dock"][0]
        self.assertEqual(dock_entry["action"],
                         {"name": "select_view", "view": "talon_apps"})
        self.assertFalse([i for i in ids if i == "home"])


if __name__ == "__main__":
    unittest.main()
