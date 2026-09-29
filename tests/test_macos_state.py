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
from macos_state import build_payload, containing, redact, window_bounds
import mac_preview
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


class TestWindowBounds(unittest.TestCase):
    def test_ax_title_match_wins(self):
        windows = [cg_window(7, "other", 0, 0, 100, 100),
                   cg_window(7, "want", 10, 10, 200, 200)]
        self.assertEqual(window_bounds(windows, 7, "want"),
                         {"x": 10, "y": 10, "w": 200, "h": 200})

    def test_other_pid_ignored(self):
        windows = [cg_window(7, "top", 0, 0, 10, 10),
                   cg_window(9, "x", 0, 0, 500, 500)]
        self.assertEqual(window_bounds(windows, 9),
                         {"x": 0, "y": 0, "w": 500, "h": 500})

    def test_nameless_top_window_fallback(self):
        # Without Screen Recording, CG strips window names (verified live
        # under launchd) but keeps bounds/order/owner: the topmost window
        # of the pid is still a real, showable rect.
        windows = [cg_window(7, "", 5, 5, 111, 222),
                   cg_window(7, "", 0, 0, 100, 100)]
        self.assertEqual(window_bounds(windows, 7),
                         {"x": 5, "y": 5, "w": 111, "h": 222})

    def test_nameless_other_pid_ignored(self):
        self.assertIsNone(window_bounds([cg_window(9, "", 0, 0, 5, 5)], 7))

    def test_windowless_pid_is_none(self):
        # Frontmost app with no windows (Safari, no open windows): honest
        # None even though another app's windows top the Z-order.
        windows = [cg_window(7, "top", 0, 0, 10, 10)]
        self.assertIsNone(window_bounds(windows, 999, ""))

    def test_string_xy_coerced(self):
        self.assertEqual(window_bounds([cg_window(7, "x", "-355", "-1049",
                                                 1920, 1049)], 7),
                         {"x": -355, "y": -1049, "w": 1920, "h": 1049})

    def test_nothing_usable_is_none(self):
        self.assertIsNone(window_bounds([], 7))
        self.assertIsNone(window_bounds(None, 7))

    def test_ns_dictionary_like_entries(self):
        # Live CGWindowList entries are NSDictionary: .get works but
        # isinstance(dict) is False. UserDict doubles for that shape.
        from collections import UserDict
        windows = [UserDict(cg_window(7, "x", 1, 2, 30, 40))]
        self.assertEqual(window_bounds(windows, 7),
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


class TestPreviewFfmpeg(unittest.TestCase):
    """Preview capture via bounded ffmpeg subprocess (bridges/mac_preview).

    subprocess.run is mocked: these prove command shape, per-display
    fan-out, cap enforcement, and skip-on-failure -- never a real
    capture. No ObjC pixel path may remain in capture_set."""

    DISPLAYS = [(1, 0.0, 0.0, 1728.0, 1117.0, True)]

    @staticmethod
    def _jpeg(w=480, h=310):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "JPEG",
                                                     quality=60)
        return buf.getvalue()

    def _run(self, stdout, returncode=0):
        import subprocess
        proc = subprocess.CompletedProcess(args=["ffmpeg"],
                                           returncode=returncode,
                                           stdout=stdout, stderr=b"")
        return proc

    def test_cmd_shape_pipe_only(self):
        from unittest import mock
        cmd = mac_preview.ffmpeg_cmd(0)
        self.assertEqual(cmd[0], mac_preview.FFMPEG_BIN)
        self.assertIn("Capture screen 0:none", cmd)
        self.assertEqual(cmd[-3:], ["-f", "mjpeg", "-"])
        self.assertNotIn("-y", cmd)  # never temp files
        with mock.patch.object(mac_preview, "active_displays",
                               return_value=list(self.DISPLAYS)):
            with mock.patch("subprocess.run",
                             return_value=self._run(self._jpeg())) as run:
                frames = mac_preview.capture_set()
        self.assertEqual(len(frames), 1)
        argv = run.call_args[0][0]
        self.assertEqual(argv, mac_preview.ffmpeg_cmd(0))
        kw = run.call_args[1]
        self.assertEqual(kw["stdout"], __import__("subprocess").PIPE)
        self.assertIsNotNone(kw.get("timeout"))

    def test_frame_dimensions_from_pipe(self):
        from unittest import mock
        with mock.patch.object(mac_preview, "active_displays",
                               return_value=list(self.DISPLAYS)):
            with mock.patch("subprocess.run",
                             return_value=self._run(self._jpeg(480, 310))):
                frames = mac_preview.capture_set()
        self.assertEqual([(f["display_index"], f["w"], f["h"])
                          for f in frames], [(0, 480, 310)])
        for f in frames:
            self.assertLessEqual(len(f["jpeg"]), 56000)

    def test_failure_skips_display(self):
        from unittest import mock
        with mock.patch.object(mac_preview, "active_displays",
                               return_value=list(self.DISPLAYS)):
            with mock.patch("subprocess.run",
                             return_value=self._run(b"", returncode=1)):
                self.assertEqual(mac_preview.capture_set(), [])
        with mock.patch.object(mac_preview, "active_displays",
                               return_value=list(self.DISPLAYS)):
            with mock.patch("subprocess.run",
                             side_effect=TimeoutError("hung")):
                self.assertEqual(mac_preview.capture_set(), [])
        self.assertIsNone(mac_preview.preview_doc([]))

    def test_over_cap_skipped(self):
        from unittest import mock
        big = b"x" * (mac_preview.FRAME_JPEG_CAP + 1)
        with mock.patch.object(mac_preview, "active_displays",
                               return_value=list(self.DISPLAYS)):
            with mock.patch("subprocess.run",
                             return_value=self._run(big)):
                self.assertEqual(mac_preview.capture_set(), [])
        self.assertIsNone(mac_preview.encode(big))

    def test_no_objc_pixel_path(self):
        import inspect
        src = inspect.getsource(mac_preview.capture_set)
        self.assertNotIn("CGDisplayCreateImage", src)
        self.assertNotIn("cg_to_pil", src)
        self.assertNotIn("CGDataProvider", src)


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
