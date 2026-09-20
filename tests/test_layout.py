"""Static-region composition (ISA D6 option B) tests.

POST /layout splits the panel into fixed regions, each bound to its own
renderer; per-region presents recomposite from a per-region frame cache, so
one region updating never disturbs the others, and a renderer crash is
contained to its region (ISA D7).

No framebuffer needed: DISPLAYD_FAKE_FB=1 selects the in-memory double
(1920x1080), so show/layout/snapshot behave identically minus photons.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from displayd import parse_layout
from PIL import Image

W, H = 1920, 1080


def _renderers():
    return displayd.load_renderers(displayd.RENDERER_DIR)


class TestParseLayout(unittest.TestCase):
    def test_acceptance_stack(self):
        regions = parse_layout(
            {"regions": [
                {"name": "top", "height": "50%",
                 "renderer": "solid", "params": {"color": "red"}},
                {"name": "bottom", "height": "50%",
                 "renderer": "solid", "params": {"color": "blue"}}]},
            W, H, _renderers())
        self.assertEqual([r["rect"] for r in regions],
                         [(0, 0, 1920, 540), (0, 540, 1920, 540)])
        self.assertEqual(regions[0]["params"], {"color": "red"})

    def test_even_split_by_default(self):
        regions = parse_layout(
            {"regions": [{"name": "a", "renderer": "solid"},
                         {"name": "b", "renderer": "solid"}]},
            W, H, _renderers())
        self.assertEqual(regions[0]["rect"], (0, 0, 1920, 540))
        self.assertEqual(regions[1]["rect"], (0, 540, 1920, 540))

    def test_widths_stack_horizontally(self):
        regions = parse_layout(
            {"regions": [{"name": "a", "width": "25%", "renderer": "solid"},
                         {"name": "b", "width": "75%", "renderer": "solid"}]},
            W, H, _renderers())
        self.assertEqual(regions[0]["rect"], (0, 0, 480, 1080))
        self.assertEqual(regions[1]["rect"], (480, 0, 1440, 1080))

    def test_grid_with_span(self):
        regions = parse_layout(
            {"rows": 2, "cols": 2, "regions": [
                {"name": "big", "row": 0, "col": 0,
                 "row_span": 2, "renderer": "solid"},
                {"name": "tr", "row": 0, "col": 1, "renderer": "solid"},
                {"name": "br", "row": 1, "col": 1, "renderer": "solid"}]},
            W, H, _renderers())
        by_name = {r["name"]: r["rect"] for r in regions}
        self.assertEqual(by_name["big"], (0, 0, 960, 1080))
        self.assertEqual(by_name["tr"], (960, 0, 960, 540))
        self.assertEqual(by_name["br"], (960, 540, 960, 540))

    def test_rect_forms(self):
        regions = parse_layout(
            {"regions": [
                {"name": "a", "renderer": "solid",
                 "rect": {"x": 0, "y": 0, "w": "50%", "h": 100}},
                {"name": "b", "renderer": "solid",
                 "rect": [960, 100, 960, 980]}]},
            W, H, _renderers())
        self.assertEqual(regions[0]["rect"], (0, 0, 960, 100))
        self.assertEqual(regions[1]["rect"], (960, 100, 960, 980))

    def test_rejections(self):
        renderers = _renderers()
        with self.assertRaises(ValueError):
            parse_layout({}, W, H, renderers)
        with self.assertRaises(ValueError):
            parse_layout({"regions": []}, W, H, renderers)
        with self.assertRaises(KeyError):
            parse_layout({"regions": [{"name": "a", "renderer": "nope"}]},
                         W, H, renderers)
        with self.assertRaises(ValueError):  # text renderer needs text
            parse_layout({"regions": [{"name": "a", "renderer": "text",
                                       "params": {}}]},
                         W, H, renderers)
        with self.assertRaises(ValueError):  # duplicate names
            parse_layout({"regions": [{"name": "a", "renderer": "solid"},
                                      {"name": "a", "renderer": "solid"}]},
                         W, H, renderers)
        with self.assertRaises(ValueError):  # bad percentage
            parse_layout({"regions": [{"name": "a", "renderer": "solid",
                                       "height": "150%"}]},
                         W, H, renderers)
        with self.assertRaises(ValueError):  # grid overlap
            parse_layout({"rows": 1, "cols": 1, "regions": [
                {"name": "a", "row": 0, "col": 0, "renderer": "solid"},
                {"name": "b", "row": 0, "col": 0, "renderer": "solid"}]},
                W, H, renderers)
        with self.assertRaises(ValueError):  # span overflow
            parse_layout({"rows": 1, "cols": 1, "regions": [
                {"name": "a", "row": 0, "col": 0,
                 "col_span": 2, "renderer": "solid"}]},
                W, H, renderers)


def _install(daemon, name, run, params=None, inputs=None):
    """Register a tiny in-memory test renderer (no file on disk)."""
    mod = types.ModuleType("test_renderer_" + name)
    mod.run = run
    daemon.renderers[name] = {
        "module": mod, "description": "test renderer", "params": params or {},
        "inputs": inputs or {}, "static": True,
    }
    for input_name, spec in (inputs or {}).items():
        daemon.feeds.declare(name, input_name, spec)


def _probe_run(screen, params, stop):
    """Feed-driven region: repaints whenever the tick input changes."""
    last = object()
    while not stop.is_set():
        vals = screen.get_input("probe", "tick")
        cur = vals[-1] if vals else None
        key = json.dumps(cur, sort_keys=True, default=str)
        if key != last:
            last = key
            color = (0, 0, 0)
            if isinstance(cur, dict) and "color" in cur:
                color = tuple(cur["color"])
            screen.present(Image.new("RGB", (screen.W, screen.H), color))
        stop.wait(0.05)


def _boom_run(screen, params, stop):
    raise RuntimeError("boom on purpose")


class LayoutDaemonTestCase(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        _install(self.daemon, "probe", _probe_run,
                 inputs={"tick": {"type": "object"}})
        _install(self.daemon, "boom", _boom_run)
        self.addCleanup(self.daemon.clear)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _wait(self, cond, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def _frame(self):
        png = self.daemon.snapshot()
        self.assertIsNotNone(png, "nothing has been drawn yet")
        return Image.open(io.BytesIO(png)).convert("RGB")

    def _pixel(self, img, x, y):
        return img.getpixel((x, y))

    def _layout(self, regions):
        return self.daemon.set_layout({"regions": regions})

    def test_two_live_regions(self):
        self._layout([
            {"name": "top", "height": "50%",
             "renderer": "solid", "params": {"color": "red"}},
            {"name": "bottom", "height": "50%",
             "renderer": "solid", "params": {"color": "blue"}}])
        self.assertTrue(
            self._wait(lambda: self._pixel(self._frame(), 960, 270)[:2] == (255, 0)
                       and self._pixel(self._frame(), 960, 810)[2] > 200),
            "both regions never painted")
        state = self.daemon.state()
        self.assertIsNone(state["renderer"])
        self.assertIsNotNone(state["layout"]["first_pixel_ms"])
        self.assertEqual([r["name"] for r in state["layout"]["regions"]],
                         ["top", "bottom"])
        self.assertEqual(state["layout"]["regions"][0]["rect"],
                         {"x": 0, "y": 0, "w": 1920, "h": 540})

    def test_show_reverts_to_single(self):
        self._layout([{"name": "top", "renderer": "solid",
                       "params": {"color": "red"}},
                      {"name": "bottom", "renderer": "solid",
                       "params": {"color": "blue"}}])
        self.daemon.show("solid", {"color": "green"})
        self.assertTrue(
            self._wait(lambda: self._pixel(self._frame(), 960, 810)[1] > 200),
            "single view never took over the panel")
        state = self.daemon.state()
        self.assertIsNone(state["layout"])
        self.assertEqual(state["renderer"], "solid")

    def test_clear_drops_layout(self):
        self._layout([{"name": "top", "renderer": "solid"},
                      {"name": "bottom", "renderer": "solid"}])
        self.daemon.clear()
        state = self.daemon.state()
        self.assertIsNone(state["layout"])
        self.assertIsNone(state["renderer"])

    def test_bad_layout_rejected_atomically(self):
        self.daemon.show("solid", {"color": "red"})
        self.assertTrue(self._wait(
            lambda: self._pixel(self._frame(), 10, 10)[0] > 200))
        with self.assertRaises(KeyError):
            self._layout([{"name": "x", "renderer": "nope"}])
        with self.assertRaises(ValueError):
            self._layout([{"name": "x", "renderer": "solid",
                           "height": "150%"}])
        state = self.daemon.state()  # undisturbed: still single red
        self.assertIsNone(state["layout"])
        self.assertEqual(state["renderer"], "solid")
        self.assertGreater(self._pixel(self._frame(), 10, 10)[0], 200)

    def test_feed_routes_to_bound_region(self):
        self._layout([
            {"name": "top", "height": "50%",
             "renderer": "solid", "params": {"color": "red"}},
            {"name": "bottom", "height": "50%", "renderer": "probe"}])
        self.assertTrue(self._wait(
            lambda: self._pixel(self._frame(), 960, 270)[0] > 200))
        self.daemon.feed("probe", "tick", {"color": [0, 255, 0]})
        self.assertTrue(self._wait(
            lambda: self._pixel(self._frame(), 960, 810)[1] > 200),
            "fed region never repainted")
        img = self._frame()  # untouched region is undisturbed
        self.assertGreater(self._pixel(img, 960, 270)[0], 200)

    def test_partial_update_keeps_other_region(self):
        """Per-region cache: the top pixels are bit-identical before and
        after the bottom region updates."""
        self._layout([
            {"name": "top", "height": "50%",
             "renderer": "solid", "params": {"color": "#010203"}},
            {"name": "bottom", "height": "50%", "renderer": "probe"}])
        self.assertTrue(self._wait(
            lambda: self.daemon.snapshot() is not None))
        before = self._frame().crop((0, 0, 1920, 540)).tobytes()
        self.daemon.feed("probe", "tick", {"color": [0, 0, 255]})
        self.assertTrue(self._wait(
            lambda: self._pixel(self._frame(), 960, 810)[2] > 200))
        after = self._frame().crop((0, 0, 1920, 540)).tobytes()
        self.assertEqual(before, after)

    def test_rotation_yields_to_layout(self):
        """An enabled playlist must not clobber an active layout: the
        manual hold fires on set_layout, and even with it explicitly
        resumed the layout-active safety net holds rotation (bar hidden)
        across full dwells."""
        self.daemon.set_policy({
            "playlist": {"enabled": True, "views": [
                {"renderer": "solid", "params": {"color": "red"},
                 "dwell": 3}]}})
        self._layout([{"name": "top", "height": "50%",
                       "renderer": "solid",
                       "params": {"color": "green"}}])
        self.daemon.playlist.resume()  # clear the manual hold on purpose
        self.assertEqual(self.daemon.playlist.status()["hold"],
                         "layout-active")
        self.assertIsNone(self.daemon.playlist.status()["progress"])
        time.sleep(3.5)  # a full dwell passes under the hold
        state = self.daemon.state()
        self.assertIsNotNone(state["layout"],
                             "rotation clobbered the layout")
        self.assertIsNone(state["renderer"])

    def test_renderer_error_contained(self):
        self._layout([
            {"name": "bad", "height": "50%", "renderer": "boom"},
            {"name": "good", "height": "50%",
             "renderer": "solid", "params": {"color": "green"}}])
        self.assertTrue(self._wait(
            lambda: self._pixel(self._frame(), 960, 810)[1] > 200),
            "healthy region never painted")
        state = self.daemon.state()
        by_name = {r["name"]: r for r in state["layout"]["regions"]}
        self.assertIn("RuntimeError", by_name["bad"]["error"])
        self.assertIsNone(by_name["good"]["error"])
        # The daemon is still alive: feeds and a fresh layout work.
        self.daemon.feed("probe", "tick", {"color": [1, 1, 1]})
        self._layout([{"name": "only", "renderer": "solid",
                       "params": {"color": "white"}}])
        self.assertTrue(self._wait(
            lambda: self._pixel(self._frame(), 960, 540)[0] > 200))


class LayoutHttpTestCase(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self._old_daemon = displayd.DAEMON
        self.addCleanup(self._restore_daemon)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        displayd.DAEMON = self.daemon
        server = ThreadingHTTPServer(("127.0.0.1", 0), displayd.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(self.daemon.clear)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _restore_daemon(self):
        displayd.DAEMON = self._old_daemon

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                if "image/" in resp.headers.get("Content-Type", ""):
                    return resp.status, raw
                return resp.status, json.loads(raw.decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def _wait_snapshot(self, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            code, _ = self.call("GET", "/snapshot")
            if code == 200:
                return True
            time.sleep(0.05)
        return False

    def test_layout_roundtrip(self):
        code, out = self.call("POST", "/layout", {"regions": [
            {"name": "top", "height": "50%",
             "renderer": "solid", "params": {"color": "red"}},
            {"name": "bottom", "height": "50%",
             "renderer": "solid", "params": {"color": "blue"}}]})
        self.assertEqual(code, 200, out)
        self.assertTrue(self._wait_snapshot(), "layout never painted")
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual([r["name"] for r in state["layout"]["regions"]],
                         ["top", "bottom"])
        self.assertEqual([r["renderer"] for r in state["layout"]["regions"]],
                         ["solid", "solid"])
        code, layout = self.call("GET", "/layout")
        self.assertEqual(code, 200)
        self.assertEqual(len(layout["layout"]["regions"]), 2)
        # A bare /show reverts to single-renderer mode.
        code, _ = self.call("POST", "/show",
                            {"renderer": "solid",
                             "params": {"color": "green"}})
        self.assertEqual(code, 200)
        code, state = self.call("GET", "/state")
        self.assertIsNone(state["layout"])
        self.assertEqual(state["renderer"], "solid")
        # DELETE /layout blanks the screen.
        code, _ = self.call("POST", "/layout", {"regions": [
            {"name": "only", "renderer": "solid"}]})
        self.assertEqual(code, 200)
        code, _ = self.call("DELETE", "/layout")
        self.assertEqual(code, 200)
        code, state = self.call("GET", "/state")
        self.assertIsNone(state["layout"])
        self.assertIsNone(state["renderer"])

    def test_layout_http_errors(self):
        code, out = self.call("POST", "/layout", {"regions": [
            {"name": "x", "renderer": "nope"}]})
        self.assertEqual(code, 404, out)
        code, out = self.call("POST", "/layout", {"regions": [
            {"name": "x", "renderer": "solid", "height": "0"}]})
        self.assertEqual(code, 400, out)
        code, out = self.call("POST", "/layout", {})
        self.assertEqual(code, 400, out)


if __name__ == "__main__":
    unittest.main()
