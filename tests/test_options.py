"""Options-view tests: the tap-anywhere landing screen.

Covers renderers/options.py (the view-selection screen every unconsumed
tap routes to): advertised through load_renderers with a valid PARAMS
schema, coerce_views fallbacks, headless frame rendering at panel and
tiny sizes, and the show -> show(clock) round-trip through a headless
DisplayDaemon (tap lands options; control-page /show is the way back).

Run from the repo root:  python3 -m unittest tests.test_options -v
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

OPT_PATH = os.path.join(displayd.RENDERER_DIR, "options.py")


def load_options(name="options_testmod"):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, OPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OPT = load_options("options_mod")


class FakeFb:
    def __init__(self, w=480, h=270):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


def make_screen(w=480, h=270):
    return displayd.Screen(FakeFb(w, h))


def non_bg_count(img):
    small = img.resize((160, 90)).convert("L")
    return sum(1 for p in small.getdata() if p > 24)


class TestOptionsAdvertised(unittest.TestCase):
    def test_loads_with_valid_schema(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("options", found)
        entry = found["options"]
        self.assertIn("module", entry, "options failed to import: %s"
                      % entry.get("broken"))
        self.assertTrue(entry["static"])
        for pname, spec in entry["params"].items():
            self.assertIn(spec.get("type"), displayd.INPUT_TYPES,
                          "options.%s has unknown type %r"
                          % (pname, spec.get("type")))

    def test_selection_params_validate(self):
        displayd.validate_params({}, OPT.PARAMS)
        displayd.validate_params(
            {"title": "Menu", "views": ["clock", "chat"]}, OPT.PARAMS)
        with self.assertRaises(ValueError):
            displayd.validate_params({"title": 7}, OPT.PARAMS)


class TestCoerceViews(unittest.TestCase):
    def test_default_is_pinned_four(self):
        self.assertEqual(OPT.coerce_views({}), ["clock", "chat", "row", "stream"])
        self.assertEqual(OPT.coerce_views(None), ["clock", "chat", "row", "stream"])
        self.assertEqual(OPT.coerce_views({"views": []}),
                         ["clock", "chat", "row", "stream"])

    def test_custom_list_kept(self):
        self.assertEqual(OPT.coerce_views({"views": ["life", "qr"]}),
                         ["life", "qr"])

    def test_malformed_entries_skipped_never_fatal(self):
        self.assertEqual(
            OPT.coerce_views({"views": ["clock", "", None, 7, "a/b", " chat "]}),
            ["clock", "chat"])
        self.assertEqual(OPT.coerce_views({"views": "clock"}),
                         ["clock", "chat", "row", "stream"])
        self.assertEqual(OPT.coerce_views({"views": ["", None]}),
                         ["clock", "chat", "row", "stream"])


class TestOptionsRenders(unittest.TestCase):
    def _run(self, params, w=480, h=270):
        screen = make_screen(w, h)
        stop = threading.Event()
        stop.set()
        OPT.run(screen, params, stop)
        self.assertEqual(len(screen.fb.frames), 1)
        return screen.fb.frames[0]

    def test_default_frame_draws(self):
        img = self._run({})
        self.assertGreater(non_bg_count(img), 100)

    def test_custom_views_draw(self):
        img = self._run({"title": "Menu", "views": ["life", "qr", "beads"]})
        self.assertGreater(non_bg_count(img), 100)

    def test_panel_size_frame_draws(self):
        img = self._run({}, w=1920, h=1080)
        self.assertEqual(img.size, (1920, 1080))
        # Lower bar than the small-screen tests: the same text covers
        # fewer downscaled pixels at panel size, but must still draw.
        self.assertGreater(non_bg_count(img), 20)

    def test_malformed_params_still_draw(self):
        img = self._run({"views": ["", None, 7], "title": None})
        self.assertGreater(non_bg_count(img), 100)


class OptionsDaemonTestCase(unittest.TestCase):
    """Headless show(options) -> show(clock): tap lands options, /show back."""

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

    def test_show_options_then_back_to_clock(self):
        # The tap-anywhere fallback POSTs /show {"renderer": "options"}:
        # it must be showable from a running view and returnable.
        self.daemon.show("clock", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "clock"))
        self.daemon.show("options", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "options"))
        # STATIC views park after one present: wait for the frame, not
        # just the view switch.
        self.assertTrue(self._wait(lambda: self.daemon.snapshot()
                                   is not None))
        img = self._snapshot()
        # Panel-size frame: same lowered bar as test_panel_size_frame_draws.
        self.assertGreater(non_bg_count(img), 20)
        # The way back: an explicit selection wins, options never traps.
        self.daemon.show("clock", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "clock"))

    def test_options_cancels_transient(self):
        # Tap during a notice/reload transient lands options: manual /show
        # cancels transients by daemon design, so no view traps the user.
        self.daemon.show("clock", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "clock"))
        self.daemon.notify("hello", duration=60)
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "notice"))
        self.daemon.show("options", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "options"))


if __name__ == "__main__":
    unittest.main()
