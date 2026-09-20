"""Touch-confidence mode tests: renderer, daemon integration, feed API.

Covers the opt-in touchscreen confidence view (renderers/touch_confidence.py)
fed by touch.py's confidence_feedback switch: visible region map, live tap
diagnostics, counters, malformed payloads, and the show -> feed -> clock
round-trip through a headless DisplayDaemon.

Run from the repo root:  python3 -m unittest tests.test_touch_confidence -v
"""

import io
import os
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from displayd import FeedStore

TC_PATH = os.path.join(displayd.RENDERER_DIR, "touch_confidence.py")


def load_tc(name="touch_confidence_testmod"):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, TC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TC = load_tc("touch_confidence_mod")

BG = (10, 10, 14)

# Mirrors the lnx-server panel split from the brief: left third screen_on,
# middle dead zone, right third playlist_next.
PANEL_REGIONS = [
    {"id": "screen-on", "rect": [0, 0, 640, 1080],
     "action": {"name": "screen_on"}},
    {"id": "playlist-next", "rect": [1280, 0, 640, 1080],
     "action": {"name": "playlist_next"}},
]


class FakeFb:
    def __init__(self, w=480, h=270):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


def make_screen(w=480, h=270):
    screen = displayd.Screen(FakeFb(w, h))
    store = FeedStore()
    store.declare("touch_confidence", "tap", TC.INPUTS["tap"])
    screen.feeds = store
    return screen, store


def non_bg_count(img):
    small = img.resize((160, 90)).convert("L")
    return sum(1 for p in small.getdata() if p > 24)


class TestRendererAdvertised(unittest.TestCase):
    def test_loads_with_tap_input(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("touch_confidence", found)
        entry = found["touch_confidence"]
        self.assertIn("module", entry)
        self.assertIn("tap", entry["inputs"])
        self.assertEqual(entry["inputs"]["tap"]["type"], "object")

    def test_tap_schema_requires_coordinates(self):
        spec = TC.INPUTS["tap"]
        displayd.validate_value({"x": 10, "y": 20}, spec, "tap")
        with self.assertRaises(ValueError):
            displayd.validate_value({"x": 10}, spec, "tap")
        with self.assertRaises(ValueError):
            displayd.validate_value({"x": "left", "y": 20}, spec, "tap")

    def test_params_accept_region_list(self):
        displayd.validate_params(
            {"regions": PANEL_REGIONS, "width": 1920, "height": 1080},
            TC.PARAMS)


class TestLabelsAndRegions(unittest.TestCase):
    def test_format_label_plain_and_object_actions(self):
        self.assertEqual(TC.format_label("screen-on", "screen_on"),
                         "screen-on \u2192 screen_on")
        self.assertEqual(TC.format_label("screen-on", {"name": "screen_on"}),
                         "screen-on \u2192 screen_on")
        self.assertEqual(TC.format_label("mystery", None),
                         "mystery \u2192 no action")

    def test_coerce_regions_scales_and_clips(self):
        boxes = TC.coerce_regions({"regions": PANEL_REGIONS,
                                   "width": 1920, "height": 1080}, 480, 270)
        self.assertEqual([b[0] for b in boxes],
                         ["screen-on", "playlist-next"])
        # Left third of 1920 -> left third of 480.
        self.assertEqual(boxes[0][1], (0, 0, 160, 270))
        self.assertEqual(boxes[1][1], (320, 0, 160, 270))
        self.assertEqual(boxes[0][2], {"name": "screen_on"})

    def test_coerce_regions_skips_malformed(self):
        params = {"regions": [
            {"id": "good", "rect": [0, 0, 10, 10], "action": "screen_on"},
            {"id": "", "rect": [0, 0, 10, 10]},          # empty id
            {"rect": [0, 0, 10, 10]},                    # missing id
            {"id": "bad-rect", "rect": [0, 0]},          # short rect
            {"id": "flat", "rect": [0, 0, 0, 10]},       # zero width
            {"id": "neg", "rect": [0, 0, -5, 10]},       # negative width
            "not-a-dict",
            None,
        ]}
        boxes = TC.coerce_regions(params, 480, 270)
        self.assertEqual([b[0] for b in boxes], ["good"])

    def test_coerce_regions_defaults_to_no_scaling(self):
        boxes = TC.coerce_regions(
            {"regions": [{"id": "a", "rect": [10, 10, 100, 50]}]}, 480, 270)
        self.assertEqual(boxes[0][1], (10, 10, 100, 50))

    def test_coerce_regions_rejects_garbage_params(self):
        self.assertEqual(TC.coerce_regions(None, 480, 270), [])
        self.assertEqual(TC.coerce_regions({"regions": "nope"}, 480, 270), [])
        self.assertEqual(TC.coerce_regions({"regions": None}, 480, 270), [])


class TestSummarize(unittest.TestCase):
    def test_empty_is_zeroed_waiting_state(self):
        summary = TC.summarize([])
        self.assertEqual((summary["total"], summary["hits"],
                          summary["misses"]), (0, 0, 0))
        self.assertEqual(summary["per_region"], {})
        self.assertIsNone(summary["last"])
        self.assertEqual(TC._last_line(summary), "waiting for taps")

    def test_hits_misses_and_per_region_counts(self):
        taps = [
            {"x": 100, "y": 500, "region": "screen-on",
             "action": "screen_on", "hit": True},
            {"x": 1500, "y": 500, "region": "playlist-next",
             "action": "playlist_next", "hit": True},
            {"x": 960, "y": 500, "hit": False, "result": "dead-zone"},
        ]
        summary = TC.summarize(taps)
        self.assertEqual((summary["total"], summary["hits"],
                          summary["misses"]), (3, 2, 1))
        self.assertEqual(summary["per_region"],
                         {"screen-on": 1, "playlist-next": 1})
        self.assertIs(summary["last"], taps[2])

    def test_hit_defaults_to_region_presence(self):
        summary = TC.summarize([{"x": 1, "y": 2, "region": "screen-on"}])
        self.assertEqual((summary["hits"], summary["misses"]), (1, 0))
        summary = TC.summarize([{"x": 960, "y": 500}])
        self.assertEqual((summary["hits"], summary["misses"]), (0, 1))

    def test_last_line_hit_and_dead_zone(self):
        hit = TC.summarize([{"x": 100, "y": 500, "region": "screen-on",
                             "action": "screen_on", "hit": True}])
        line = TC._last_line(hit)
        self.assertIn("100", line)
        self.assertIn("screen-on", line)
        self.assertIn("screen_on", line)
        miss = TC.summarize([{"x": 960, "y": 500, "hit": False}])
        line = TC._last_line(miss)
        self.assertIn("DEAD ZONE", line)
        self.assertIn("no action", line)


class TestValidTaps(unittest.TestCase):
    def test_skips_garbage_but_keeps_valid(self):
        class GarbageScreen:
            def get_input(self, renderer, input_name):
                self.seen = (renderer, input_name)
                return ["junk", None, 42,
                        {"x": "left", "y": 5},   # wrong types
                        {"x": True, "y": 5},     # bools are not coordinates
                        {"x": 5},                # missing y
                        {"x": 5, "y": 6}]        # the one good tap

        screen = GarbageScreen()
        taps = TC.valid_taps(screen)
        self.assertEqual(screen.seen, ("touch_confidence", "tap"))
        self.assertEqual(taps, [{"x": 5, "y": 6}])

    def test_feed_errors_read_empty(self):
        class BrokenScreen:
            def get_input(self, *a):
                raise RuntimeError("store gone")

        self.assertEqual(TC.valid_taps(BrokenScreen()), [])


class TestDraw(unittest.TestCase):
    def _draw(self, regions, taps, w=480, h=270):
        screen, _ = make_screen(w, h)
        params = {"regions": regions}
        if regions is not None:
            boxes = TC.coerce_regions(params, w, h)
        else:
            boxes = []
        return TC.draw(screen, TC.DEFAULT_TITLE, TC.DEFAULT_INSTRUCTIONS,
                       boxes, TC.summarize(taps), BG)

    def test_initial_state_is_painted_not_blank(self):
        img = self._draw(PANEL_REGIONS, [])
        self.assertEqual(img.size, (480, 270))
        self.assertGreater(non_bg_count(img), 60)

    def test_regions_are_visible_as_colored_boxes(self):
        img = self._draw(PANEL_REGIONS, [])
        # First box starts at the top-left of the map band; its outline
        # must read back as the first palette colour, not background.
        map_top = int(270 * 0.26)
        self.assertEqual(img.getpixel((1, map_top + 2))[:3],
                         TC.REGION_COLORS[0])

    def test_labels_change_pixels(self):
        # Same boxes, different actions: only the label text differs, so a
        # pixel difference proves the labels are actually drawn.
        plain = [{"id": "screen-on", "rect": [0, 0, 640, 1080]}]
        named = [{"id": "screen-on", "rect": [0, 0, 640, 1080],
                  "action": "screen_on"}]
        a = self._draw(plain, []).tobytes()
        b = self._draw(named, []).tobytes()
        self.assertNotEqual(a, b)

    def test_region_sets_render_differently(self):
        a = self._draw(PANEL_REGIONS, []).tobytes()
        b = self._draw([], []).tobytes()
        self.assertNotEqual(a, b)

    def test_tap_updates_frame(self):
        before = self._draw(PANEL_REGIONS, []).tobytes()
        tap = {"x": 100, "y": 500, "region": "screen-on",
               "action": "screen_on", "hit": True, "result": "dispatched"}
        after = self._draw(PANEL_REGIONS, [tap]).tobytes()
        self.assertNotEqual(before, after)

    def test_dead_zone_updates_frame(self):
        before = self._draw(PANEL_REGIONS, []).tobytes()
        miss = {"x": 960, "y": 500, "hit": False, "result": "dead-zone"}
        after = self._draw(PANEL_REGIONS, [miss]).tobytes()
        self.assertNotEqual(before, after)

    def test_counters_change_frame(self):
        one = self._draw(PANEL_REGIONS, [{"x": 1, "y": 2,
                                          "hit": False}]).tobytes()
        two = self._draw(PANEL_REGIONS, [{"x": 1, "y": 2, "hit": False},
                                         {"x": 3, "y": 4,
                                          "hit": False}]).tobytes()
        self.assertNotEqual(one, two)

    def test_error_result_renders(self):
        taps = [{"x": 100, "y": 500, "region": "screen-on",
                 "action": "screen_on", "hit": True,
                 "result": "dispatch-error", "error": "connection refused"}]
        img = self._draw(PANEL_REGIONS, taps)
        self.assertEqual(img.size, (480, 270))
        self.assertGreater(non_bg_count(img), 60)

    def test_malformed_taps_never_crash_draw(self):
        screen, _ = make_screen()
        boxes = TC.coerce_regions({"regions": PANEL_REGIONS}, 480, 270)
        garbage = ["junk", None, {"x": "s", "y": 1},
                   {"region": "screen-on"},  # no coordinates
                   {"x": 100, "y": 135, "region": "screen-on",
                    "action": {"name": "screen_on"}, "hit": True,
                    "result": "dispatched",
                    "extra_future_field": {"nested": [1, 2, 3]}}]
        summary = TC.summarize([t for t in garbage
                                if isinstance(t, dict)])
        img = TC.draw(screen, "T", "i", boxes, summary, BG)
        self.assertEqual(img.size, (480, 270))


class TestRunLoop(unittest.TestCase):
    def _run(self, screen, params, stop):
        thread = threading.Thread(target=TC.run, args=(screen, params, stop),
                                  daemon=True)
        thread.start()
        return thread

    def _wait_frames(self, screen, n, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if len(screen.fb.frames) >= n:
                return True
            time.sleep(0.05)
        return False

    def test_presents_initial_state_then_taps(self):
        screen, store = make_screen()
        spec = TC.INPUTS["tap"]
        stop = threading.Event()
        thread = self._run(screen, {"regions": PANEL_REGIONS}, stop)
        try:
            self.assertTrue(self._wait_frames(screen, 1),
                            "confidence view never painted its initial frame")
            first = screen.fb.frames[-1].tobytes()
            store.push("touch_confidence", "tap",
                       {"x": 100, "y": 135, "region": "screen-on",
                        "action": "screen_on", "hit": True,
                        "result": "dispatched"}, spec)
            self.assertTrue(self._wait_frames(screen, 2),
                            "fed tap never repainted the frame")
            second = screen.fb.frames[-1].tobytes()
            self.assertNotEqual(first, second,
                                "tap left the rendered frame unchanged")
            # A dead-zone tap must visibly change the frame again.
            store.push("touch_confidence", "tap",
                       {"x": 240, "y": 135, "hit": False,
                        "result": "dead-zone"}, spec)
            self.assertTrue(self._wait_frames(screen, 3),
                            "dead-zone tap never repainted the frame")
            third = screen.fb.frames[-1].tobytes()
            self.assertNotEqual(second, third)
        finally:
            stop.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_every_tap_changes_the_frame(self):
        screen, store = make_screen()
        spec = TC.INPUTS["tap"]
        stop = threading.Event()
        thread = self._run(screen, {"regions": PANEL_REGIONS}, stop)
        try:
            self.assertTrue(self._wait_frames(screen, 1))
            seen = [screen.fb.frames[-1].tobytes()]
            for i in range(3):
                store.push("touch_confidence", "tap",
                           {"x": 10 + i, "y": 20, "hit": False,
                            "result": "dead-zone"}, spec)
                self.assertTrue(self._wait_frames(screen, len(seen) + 1),
                                "tap %d never repainted" % i)
                seen.append(screen.fb.frames[-1].tobytes())
            for a, b in zip(seen, seen[1:]):
                self.assertNotEqual(a, b)
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_garbage_params_still_paint_and_stop_clean(self):
        screen, _ = make_screen()
        stop = threading.Event()
        thread = self._run(screen, {"regions": "nope"}, stop)
        try:
            self.assertTrue(self._wait_frames(screen, 1),
                            "garbage params must still paint a placeholder")
        finally:
            stop.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_preset_stop_exits_without_painting(self):
        screen, _ = make_screen()
        stop = threading.Event()
        stop.set()
        TC.run(screen, {"regions": PANEL_REGIONS}, stop)
        self.assertEqual(screen.fb.frames, [])


class ConfidenceDaemonTestCase(unittest.TestCase):
    """Headless show -> feed -> clock round-trip for touch_confidence."""

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

    def _wait(self, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def _snapshot(self):
        png = self.daemon.snapshot()
        self.assertIsNotNone(png, "nothing has been drawn yet")
        return Image.open(io.BytesIO(png)).convert("RGB")

    def test_advertised_with_tap_input(self):
        entry = self.daemon.renderers.get("touch_confidence")
        self.assertIsNotNone(entry)
        self.assertIn("module", entry)
        self.assertIn("tap", entry.get("inputs") or {})

    def test_show_feed_and_return_to_clock(self):
        self.daemon.show("touch_confidence",
                         {"regions": PANEL_REGIONS,
                          "width": 1920, "height": 1080})
        self.assertEqual(self.daemon.current, "touch_confidence")
        self.assertTrue(self._wait(lambda: self.daemon.snapshot() is not None),
                        "confidence view never drew its initial frame")
        before = self.daemon.snapshot()
        self.daemon.feed("touch_confidence", "tap",
                         {"x": 100, "y": 500, "region": "screen-on",
                          "action": "screen_on", "hit": True,
                          "result": "dispatched"})
        self.assertTrue(
            self._wait(lambda: self.daemon.snapshot() != before),
            "fed tap never changed the confidence frame")
        # Feeds route to the buffer; they never steal the screen.
        self.assertEqual(self.daemon.current, "touch_confidence")
        health = self.daemon.feeds.snapshot()["touch_confidence"]["tap"]
        self.assertEqual(health["count"], 1)
        self.assertEqual(health["health"], "warm")
        # And back to the clock, undisturbed.
        self.daemon.show("clock", {})
        self.assertEqual(self.daemon.current, "clock")
        self.assertTrue(self._wait(lambda: self.daemon.snapshot() is not None),
                        "clock never redrew after confidence mode")

    def test_feed_while_on_clock_leaves_clock_up(self):
        self.daemon.show("clock", {})
        self.assertTrue(self._wait(lambda: self.daemon.snapshot() is not None))
        self.daemon.feed("touch_confidence", "tap",
                         {"x": 960, "y": 500, "hit": False,
                          "result": "dead-zone"})
        time.sleep(0.5)  # a full poll window: nothing should switch
        self.assertEqual(self.daemon.current, "clock")

    def test_malformed_feed_rejected_without_side_effects(self):
        self.daemon.show("touch_confidence", {"regions": PANEL_REGIONS})
        with self.assertRaises(ValueError):
            self.daemon.feed("touch_confidence", "tap", {"x": 100})
        with self.assertRaises(ValueError):
            self.daemon.feed("touch_confidence", "tap",
                             {"x": "left", "y": 500})
        with self.assertRaises(KeyError):
            self.daemon.feed("touch_confidence", "nope", {"x": 1, "y": 2})
        with self.assertRaises(KeyError):
            self.daemon.feed("no_such_view", "tap", {"x": 1, "y": 2})
        health = self.daemon.feeds.snapshot()["touch_confidence"]["tap"]
        self.assertEqual(health["count"], 0)
        self.assertEqual(self.daemon.current, "touch_confidence")

    def test_show_rejects_bad_regions_param(self):
        self.daemon.show("clock", {})
        with self.assertRaises(ValueError):
            self.daemon.show("touch_confidence", {"regions": "nope"})
        self.assertEqual(self.daemon.current, "clock")


if __name__ == "__main__":
    unittest.main()
