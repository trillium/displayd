"""macOS state bridge + renderer (bridges/macos_state.py,
renderers/macbook.py, renderers/macbook_map.py) tests.

Proves the wire contract at three levels: the poller's pure helpers
(redact/contain/pick/build) against scripted inputs, the bridge-to-daemon
schema contract (every payload validates against the renderer's real
INPUTS spec), and a live push through the real macbook renderer into a
headless daemon feed.

Run from the repo root:  python3 -m unittest tests.test_macos_state -v
"""

import logging
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
from displayd import FeedStore, HeadlessFramebuffer, Screen, validate_value

import macos_state
from macos_state import build_payload, containing, pick_window, redact
import macbook_map


def good_payload(**kw):
    payload = build_payload(
        ts=1789800900.0, trusted=True, app_name="WezTerm",
        bundle_id="com.github.wez.wezterm", pid=3067,
        title="[1/2] macbookpro: 2M",
        bounds={"x": 0, "y": -1049, "w": 1920, "h": 1049},
        focus_display=1, mouse=(464, -283), mouse_display=1,
        displays=[(0, 0, 1728, 1117, True),
                  (-355, -1080, 1920, 1080, False)],
        talon={"mode": "command", "microphone": "RODE", "muted": False})
    payload.update(kw)
    return payload


def cg_window(pid, name, x, y, w, h, layer=0):
    return {"kCGWindowOwnerPID": pid, "kCGWindowLayer": layer,
            "kCGWindowName": name,
            "kCGWindowBounds": {"X": x, "Y": y,
                                "Width": w, "Height": h}}


class TestRedact(unittest.TestCase):
    def test_plain_title_truncated(self):
        self.assertEqual(redact("x" * 200), "x" * macos_state.TITLE_CHARS)

    def test_denied_bundle_redacted(self):
        self.assertEqual(redact("my vault", "com.1password.1password"),
                         macos_state.REDACTED)

    def test_bankish_title_redacted(self):
        for title in ("Chase checking", "ledger live", "api token=abc"):
            self.assertEqual(redact(title), macos_state.REDACTED)

    def test_non_string_never_raises(self):
        for raw in (None, 42, ["x"], {"t": 1}):
            self.assertEqual(redact(raw), "")

    def test_clean_title_passes(self):
        self.assertEqual(redact("[1/2] macbookpro: fm-"),
                         "[1/2] macbookpro: fm-")


class TestContaining(unittest.TestCase):
    DISPLAYS = [{"x": 0, "y": 0, "w": 1728, "h": 1117},
                {"x": -355, "y": -1080, "w": 1920, "h": 1080}]

    def test_each_screen_found(self):
        self.assertEqual(containing(100, 100, self.DISPLAYS), 0)
        self.assertEqual(containing(960, -524, self.DISPLAYS), 1)

    def test_outside_is_none(self):
        self.assertIsNone(containing(5000, 5000, self.DISPLAYS))

    def test_negative_y_arrangement(self):
        # TV sits above the builtin: negative y is a real screen, not void.
        self.assertEqual(containing(-355, -1080, self.DISPLAYS), 1)

    def test_malformed_displays_skipped(self):
        displays = self.DISPLAYS + [{"x": "junk"}, None, "nope"]
        self.assertEqual(containing(100, 100, displays), 0)


class TestPickWindow(unittest.TestCase):
    def test_ax_title_match_wins(self):
        windows = [cg_window(7, "other", 0, 0, 100, 100),
                   cg_window(7, "want", 10, 10, 200, 200)]
        self.assertEqual(pick_window(windows, 7, "want"),
                         {"x": 10, "y": 10, "w": 200, "h": 200})

    def test_talon_canvas_and_chrome_ignored(self):
        windows = [cg_window(7, "canvas", 0, 0, 1920, 1080, layer=1500),
                   cg_window(7, "want", 10, 10, 200, 200)]
        self.assertEqual(pick_window(windows, 7)["w"], 200)

    def test_other_pid_ignored(self):
        self.assertIsNone(pick_window([cg_window(9, "x", 0, 0, 5, 5)], 7))

    def test_string_xy_coerced(self):
        windows = [cg_window(7, "x", "-355", "-1049", 1920, 1049)]
        self.assertEqual(pick_window(windows, 7),
                         {"x": -355, "y": -1049, "w": 1920, "h": 1049})

    def test_nothing_usable_is_none(self):
        self.assertIsNone(pick_window([], 7))
        self.assertIsNone(pick_window(None, 7))

    def test_ns_dictionary_like_entries(self):
        # Live CGWindowList entries are NSDictionary: .get works but
        # isinstance(dict) is False. UserDict doubles for that shape.
        from collections import UserDict
        windows = [UserDict(cg_window(7, "x", 1, 2, 30, 40))]
        self.assertEqual(pick_window(windows, 7),
                         {"x": 1, "y": 2, "w": 30, "h": 40})


class TestBuildPayload(unittest.TestCase):
    def test_absent_omitted_never_null(self):
        payload = build_payload(app_name="Finder")
        self.assertNotIn("window_title", payload["focus"])
        self.assertNotIn("window_bounds", payload["focus"])
        self.assertNotIn("display_index", payload["focus"])
        self.assertNotIn("mouse", payload)
        self.assertNotIn("talon", payload)

    def test_degraded_app_only(self):
        payload = build_payload(trusted=False, app_name="Finder")
        self.assertEqual(payload["accessibility_trusted"], False)
        self.assertEqual(payload["focus"]["app_name"], "Finder")


class TestMap(unittest.TestCase):
    DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
                 "main": True},
                {"bounds": {"x": -355, "y": -1080, "w": 1920, "h": 1080},
                 "main": False}]

    def test_union_spans_both(self):
        self.assertEqual(macbook_map.union(self.DISPLAYS),
                         (-355.0, -1080.0, 2083.0, 2197.0))

    def test_union_empty_is_none(self):
        self.assertIsNone(macbook_map.union([]))
        self.assertIsNone(macbook_map.union([{"bounds": {"x": 1}}]))

    def test_fit_preserves_aspect_and_fits(self):
        box = macbook_map.union(self.DISPLAYS)
        scale, ox, oy = macbook_map.fit(box, 1000, 800)
        self.assertGreater(scale, 0)
        for d in self.DISPLAYS:
            rect = macbook_map.rect(d["bounds"], scale, ox, oy)
            self.assertTrue(8 <= rect[0] and rect[2] <= 1000 - 8)
            self.assertTrue(8 <= rect[1] and rect[3] <= 800 - 8)

    def test_window_and_pointer_project_inside(self):
        box = macbook_map.union(self.DISPLAYS)
        scale, ox, oy = macbook_map.fit(box, 1000, 800)
        rect = macbook_map.rect({"x": 0, "y": -1049, "w": 1920, "h": 1049},
                                scale, ox, oy)
        self.assertLess(rect[0], rect[2])
        px, py = macbook_map.project(464, -283, scale, ox, oy)
        self.assertTrue(0 <= px <= 1000 and 0 <= py <= 800)

    def test_labels(self):
        self.assertEqual(macbook_map.label(0, True), "D1(menu)")
        self.assertEqual(macbook_map.label(1, False), "D2")


class TestContract(unittest.TestCase):
    """Every poller payload must pass the daemon's real validation for
    the renderer's real INPUTS spec."""

    @classmethod
    def setUpClass(cls):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        assert "macbook" in found, "macbook renderer not advertised"
        cls.spec = found["macbook"]["inputs"]["state"]

    def test_inputs_advertised(self):
        self.assertIn("ts", self.spec["required"])
        self.assertIn("focus", self.spec["required"])

    def test_payloads_validate(self):
        cases = [good_payload(),
                 build_payload(trusted=False, app_name="Finder"),
                 build_payload(app_name="X", title="t", mouse=(1, 2),
                               talon={"mode": "sleep", "muted": True})]
        for payload in cases:
            validate_value(payload, self.spec, "macbook.state")

    def test_missing_focus_rejected(self):
        with self.assertRaises(ValueError):
            validate_value({"ts": 1.0}, self.spec, "macbook.state")


class TestRendererLive(unittest.TestCase):
    def _screen(self):
        screen = Screen(HeadlessFramebuffer(width=640, height=360))
        screen.feeds = FeedStore()
        return screen

    def test_state_reaches_real_renderer(self):
        screen = self._screen()
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        macbook = found["macbook"]["module"]
        spec = found["macbook"]["inputs"]["state"]
        screen.feeds.push("macbook", "state", good_payload(), spec)
        stop = threading.Event()
        runner = threading.Thread(target=macbook.run,
                                  args=(screen, {}, stop), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while screen.fb.last_frame is None and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        runner.join(5)
        self.assertIsNotNone(screen.fb.last_frame,
                             "renderer never presented the pushed state")

    def test_cold_renderer_presents_waiting_frame(self):
        screen = self._screen()
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        macbook = found["macbook"]["module"]
        stop = threading.Event()
        runner = threading.Thread(target=macbook.run,
                                  args=(screen, {}, stop), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while screen.fb.last_frame is None and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        runner.join(5)
        self.assertIsNotNone(screen.fb.last_frame,
                             "cold renderer left the panel blank")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
