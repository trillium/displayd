"""Talon app-switcher tests: renderer geometry, daemon focus slot,
closed touch action, bridge helpers, and Talon-side pure logic.

Run from the repo root:  python3 -m unittest tests.test_talon_apps -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
import touch
from touch import action_request, resolve_action


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REPO = os.path.join(os.path.dirname(__file__), os.pardir)
RENDERER = load_module("test_talon_apps_renderer",
                       os.path.join(REPO, "renderers", "talon_apps.py"))
BRIDGE = load_module("test_talon_apps_bridge",
                     os.path.join(REPO, "bridges", "talon_apps.py"))
TALON_SIDE = load_module(
    "test_displayd_apps_talon",
    os.path.expanduser("~/.talon/user/trillium_talon/core/"
                       "displayd_apps/displayd_apps.py"))


class FakeFramebuffer(displayd.Framebuffer):
    """Real PIL frames, fake wires (mirrors test_playlist.py)."""

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

    def raw(self, data):
        self.last_frame = data

    def repaint(self):
        pass


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

    def feed_apps(self, daemon, apps=("Safari", "Terminal", "Mail"),
                  focused="Safari"):
        daemon.feed("talon_apps", "state",
                    {"ts": time.time(), "apps": list(apps),
                     "focused": focused})


class RendererTest(unittest.TestCase):
    def test_view_retired_not_advertised(self):
        # One feature only: the standalone app-list view is gone from
        # GET /renderers, and the helper module has no run() so the
        # loader would skip it even by file name.
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertNotIn("talon_apps", found)
        self.assertIn("macbook", found)
        self.assertFalse(hasattr(RENDERER, "run"))

    def test_feed_namespace_survives_retirement(self):
        # The Mac-side poller still posts /feed/talon_apps/state: the
        # merged feature owns that namespace (feed compat), so no
        # Mac-side change was needed. Validated, stored, served.
        _real = displayd.Framebuffer
        displayd.Framebuffer = FakeFramebuffer
        self.addCleanup(setattr, displayd, "Framebuffer", _real)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        owner = displayd.DisplayDaemon(
            policy_path=os.path.join(tmp.name, "policy.json"))
        self.addCleanup(owner.clear)
        doc = {"ts": time.time(), "apps": ["Safari"],
               "focused": "Safari"}
        pushed = owner.feed("talon_apps", "state", doc)
        self.assertTrue(pushed["feed"]["count"] >= 1)
        self.assertEqual(owner.feeds.get("talon_apps", "state")[-1],
                         doc)
        status = owner.feeds.status("talon_apps", "state")
        self.assertEqual(status["health"], "warm")
        self.assertEqual(status["latest"], doc)
        with self.assertRaises(ValueError):
            owner.feed("talon_apps", "state", {"apps": []})

    def test_clean_bounds_untrusted_names(self):
        self.assertEqual(RENDERER.clean("Safari"), "Safari")
        self.assertEqual(RENDERER.clean("  \x00\x07evil\x1b  "), "evil")
        self.assertEqual(RENDERER.clean(None), "")
        self.assertEqual(RENDERER.clean(123), "")
        long_name = "x" * 200
        self.assertEqual(len(RENDERER.clean(long_name)),
                         RENDERER.NAME_CHARS)

    def _state(self, apps=("Safari", "Terminal", "Mail")):
        return {"ts": 1.0, "apps": list(apps), "focused": "Safari"}

    def test_chip_hit_follows_tab_window(self):
        # The header shows a scrolling window: with 8 apps and start=0
        # slots 0..5 hit and the strip's right overflow zone misses;
        # paging to start 2 scrolls the tail on screen.
        import macbook_layout as layout
        w = 1920
        apps = ["App%d" % i for i in range(8)]
        x0, y0, _, _ = layout.chip_rect(0, w)
        self.assertEqual(layout.chip_hit(x0 + 2, y0 + 2, w, apps, 0), 0)
        x5, y5, _, _ = layout.chip_rect(5, w)
        self.assertEqual(layout.chip_hit(x5 + 2, y5 + 2, w, apps, 0), 5)
        ax, _, aw, _ = layout.chip_area(w)
        self.assertIsNone(
            layout.chip_hit(ax + aw + 30, y0 + 2, w, apps, 0))
        start, slots = layout.page_slots(2, 8)
        self.assertIn(7, slots)
        self.assertEqual(start, 2)
        pos = slots.index(7)
        xa, ya, _, _ = layout.chip_rect(pos, w)
        self.assertEqual(layout.chip_hit(xa + 2, ya + 2, w, apps, 2), 7)
        # Header misses and garbage never raise.
        self.assertIsNone(layout.chip_hit(960, 10, w, apps, 0))
        self.assertIsNone(layout.chip_hit(960, 500, w, apps, 0))
        self.assertIsNone(layout.chip_hit(100, y0 + 2, w, [], 0))
        self.assertIsNone(layout.chip_hit(100, y0 + 2, w, None, 0))

    def test_draw_and_hit_cannot_drift(self):
        # Every drawable chip hit-tests to itself on a real size: the
        # renderer draws chip_rect, the daemon maps chip_hit, the touch
        # regions cover focus_region -- all three from macbook_layout.
        import macbook_layout as layout
        w = 1920
        apps = ["App%d" % i for i in range(10)]
        for start in (0, 2, 4):
            _, slots = layout.page_slots(start, len(apps))
            for pos, index in enumerate(slots):
                x, y, rw, rh = layout.chip_rect(pos, w)
                self.assertEqual(layout.chip_hit(x + 5, y + 5, w, apps,
                                                 start), index)


class FocusSlotTest(DaemonCase):
    def _chip(self, pos, w=1920):
        import macbook_layout as layout
        x, y, cw, ch = layout.chip_rect(pos, w)
        return int(x + cw / 2), int(y + ch / 2)

    def test_tap_queues_feed_named_command(self):
        # Header strip, tab 0: three apps all visible; chip taps queue
        # the feed-listed name at the tapped index.
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        self.feed_apps(daemon)
        result = daemon.request_focus_move(*self._chip(2))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["command"]["app"], "Mail")
        self.assertEqual(result["command"]["index"], 2)
        left = daemon.request_focus_move(*self._chip(0))
        self.assertTrue(left["ok"], left)
        self.assertEqual(left["command"]["app"], "Safari")

    def test_tap_follows_tab_window(self):
        # Paging the strip scrolls the window: the tail app is
        # unreachable at start 0 and tappable after paging to it.
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        apps = tuple("App%d" % i for i in range(8))
        self.feed_apps(daemon, apps=apps, focused="App0")
        import macbook_layout as layout
        x, y, cw, ch = layout.chip_rect(0, 1920)
        missed = daemon.request_focus_move(int(x + 2), int(y + 2))
        # Chip 0 at start 0 is App0, not the tail.
        self.assertTrue(missed["ok"])
        self.assertEqual(missed["command"]["app"], "App0")
        stepped = daemon.request_tab_step(1)
        self.assertTrue(stepped["ok"])
        self.assertEqual(stepped["tab"], 2)
        _, slots = layout.page_slots(2, len(apps))
        pos = slots.index(7)
        hit = daemon.request_focus_move(*self._chip(pos))
        self.assertTrue(hit["ok"], hit)
        self.assertEqual(hit["command"]["app"], "App7")

    def test_name_comes_from_feed_never_caller(self):
        # The body carries pixels only: there is no parameter that could
        # smuggle a name, so a tap can only select a listed app.
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        self.feed_apps(daemon, apps=["Safari"])
        import inspect
        params = inspect.signature(
            daemon.request_focus_move).parameters
        self.assertEqual(list(params), ["px", "py"])

    def test_view_gated(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        self.feed_apps(daemon)
        result = daemon.request_focus_move(*self._chip(0))
        self.assertFalse(result["ok"])
        self.assertIn("not showing", result["reason"])

    def test_aim_mode_gated(self):
        # The strip lives in GLANCE: in AIM the same pixel is review,
        # never focus -- refuse, never mis-focus.
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        self.feed_apps(daemon)
        daemon.show("macbook", {"mode": "aim"})
        result = daemon.request_focus_move(*self._chip(0))
        self.assertFalse(result["ok"])
        self.assertIn("GLANCE", result["reason"])

    def test_stale_feed_refused(self):
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        daemon.feed("talon_apps", "state",
                    {"ts": time.time() - 60, "apps": ["Safari"],
                     "focused": "Safari"})
        result = daemon.request_focus_move(*self._chip(0))
        self.assertFalse(result["ok"])
        self.assertIn("fresh", result["reason"])

    def test_strip_miss_refused(self):
        # Stepper pixels are tab actions, not focus taps: above the
        # strip is a miss, refused rather than focused elsewhere.
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        self.feed_apps(daemon)
        result = daemon.request_focus_move(960, 10)
        self.assertFalse(result["ok"])
        self.assertIn("chips", result["reason"])

    def test_malformed_coords_raise(self):
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        self.feed_apps(daemon)
        for bad in (("200", 300), (200.5, 300), (True, 300),
                    (-1, 300), (99999, 300)):
            with self.assertRaises(ValueError):
                daemon.request_focus_move(*bad)

    def test_take_focus_move_since_and_ttl(self):
        daemon = self.make_daemon()
        daemon.show("macbook", {})
        self.feed_apps(daemon)
        result = daemon.request_focus_move(*self._chip(0))
        cmd = result["command"]
        self.assertEqual(daemon.take_focus_move(), cmd)
        self.assertEqual(daemon.take_focus_move(cmd["ts"]), None)
        self.assertEqual(daemon.take_focus_move(cmd["ts"] - 0.001), cmd)
        daemon.focus_pending["ts"] = time.time() - 60
        self.assertIsNone(daemon.take_focus_move())

    def test_http_routes(self):
        import inspect
        get_src = inspect.getsource(displayd.Handler.do_GET)
        post_src = inspect.getsource(displayd.Handler.do_POST)
        self.assertIn("/talon/focus", get_src)
        self.assertIn("/talon/focus", post_src)


class TouchActionTest(unittest.TestCase):
    def test_table_entry_is_closed(self):
        spec = touch.ACTION_TABLE["talon_focus"]
        self.assertEqual(spec["method"], "POST")
        self.assertEqual(spec["path"], "/talon/focus")

    def test_config_time_ok_without_coords(self):
        method, path, body = action_request(
            {"name": "talon_focus"}, allow_missing_coords=True)
        self.assertEqual((method, path, body),
                         ("POST", "/talon/focus", {}))

    def test_dispatch_requires_tap_coords(self):
        self.assertIsNone(resolve_action({"name": "talon_focus"}))

    def test_dispatch_posts_stamped_point(self):
        resolved = resolve_action({"name": "talon_focus", "x": 200,
                                   "y": 300},
                                  panel=(1920, 1080))
        self.assertEqual(resolved,
                         ("POST", "/talon/focus", {"x": 200, "y": 300}))

    def test_malformed_and_off_panel_refused_never_clamped(self):
        for bad in ({"name": "talon_focus", "x": "200", "y": 300},
                    {"name": "talon_focus", "x": 200.5, "y": 300},
                    {"name": "talon_focus", "x": True, "y": 300},
                    {"name": "talon_focus", "x": -1, "y": 300},
                    {"name": "talon_focus", "x": 5000, "y": 300}):
            self.assertIsNone(resolve_action(bad, panel=(1920, 1080)),
                              "must refuse %r" % (bad,))

    def test_region_tap_stamps_coords(self):
        cfg = touch.default_config()
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": {"x_min": 0, "x_max": 4095,
                                    "y_min": 0, "y_max": 4095},
                    "tap_max_seconds": 60, "debounce_seconds": 0,
                    "tap_options": {"enabled": False},
                    "regions": [
                        {"id": "apps", "rect": [48, 250, 1824, 720],
                         "action": {"name": "talon_focus"}}]})
        posted = []

        class FakeClient(touch.DisplaydClient):
            def post(self, path, body):
                posted.append((path, body))
                return 200, {"ok": True}

        svc = touch.TouchService(
            cfg, client=FakeClient("http://127.0.0.1:9"))
        # Raw device coords mapping to display (200, 300): inside the
        # apps region (raw = display TILE * 4095, cf. test_touch.py).
        svc.handle_frame([touch.TouchEvent("down", 0, 426, 1137)],
                         dry_run=False)
        svc.handle_frame([touch.TouchEvent("up", 0, 426, 1137)],
                         dry_run=False)
        # Every valid tap first dismisses reload (POST /touch/tap,
        # a server-side no-op here); the region action follows it.
        focus = [p for p in posted if p[0] == "/talon/focus"]
        self.assertEqual(len(focus), 1)
        self.assertEqual(focus[0][1], {"x": 200, "y": 300})


class BridgeHelperTest(unittest.TestCase):
    def test_load_state_bounds_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(BRIDGE.load_state(tmp), (None, None))
            bad = os.path.join(tmp, "apps_state.json")
            with open(bad, "w") as fh:
                fh.write("not json")
            self.assertEqual(BRIDGE.load_state(tmp), (None, None))
            with open(bad, "w") as fh:
                json.dump({"apps": ["A", "", None, 42,
                                    "\x00B" + "y" * 100]}, fh)
            _, doc = BRIDGE.load_state(tmp)
            self.assertEqual(doc["apps"], ["A", "B" + "y" * 47])
            with open(bad, "w") as fh:
                json.dump({"apps": ["ok"], "focused": "ok"}, fh)
            _, doc = BRIDGE.load_state(tmp)
            self.assertEqual(doc["focused"], "ok")

    def test_run_focus_roundtrip_with_fake_talon(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = {"id": 7, "app": "Safari", "ts": time.time()}

            def fake_talon():
                for _ in range(200):
                    req = os.path.join(tmp, "focus_request.json")
                    if os.path.exists(req):
                        with open(req) as fh:
                            body = json.load(fh)
                        with open(os.path.join(
                                tmp, "focus_response.json"),
                                "w") as fh:
                            json.dump({"id": body["id"], "ok": True,
                                       "focused": body["name"]}, fh)
                        return
                    time.sleep(0.01)

            worker = threading.Thread(target=fake_talon)
            worker.start()
            resp = BRIDGE.run_focus(tmp, cmd)
            worker.join(timeout=10)
            self.assertEqual(resp, {"id": 7, "ok": True,
                                    "focused": "Safari"})

    def test_tick_posts_on_change_then_heartbeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "apps_state.json"),
                       "w") as fh:
                json.dump({"apps": ["Safari"], "focused": "Safari"},
                          fh)
            posted = []
            real_post, real_fetch = BRIDGE.post_feed, BRIDGE.fetch_focus
            BRIDGE.post_feed = lambda base, doc: posted.append(doc) or True
            BRIDGE.fetch_focus = lambda base, since=0.0, wait=0.0: None
            try:
                seen = [None, 0.0, 0.0]
                hb = BRIDGE.HEARTBEAT
                BRIDGE.tick("http://127.0.0.1:9", tmp, seen, now=100.0)
                self.assertEqual(len(posted), 1)  # change posts
                self.assertEqual(seen[0] is not None, True)
                BRIDGE.tick("http://127.0.0.1:9", tmp, seen,
                            now=100.0 + hb / 2)
                self.assertEqual(len(posted), 1)  # quiet: no re-post
                BRIDGE.tick("http://127.0.0.1:9", tmp, seen,
                            now=100.0 + hb + 0.1)
                self.assertEqual(len(posted), 2)  # heartbeat re-posts
                self.assertEqual(posted[1]["ts"],
                                 100.0 + hb + 0.1)  # bridge clock
            finally:
                BRIDGE.post_feed, BRIDGE.fetch_focus = \
                    real_post, real_fetch

    def test_fetch_focus_reads_daemon_slot(self):
        seen = {}

        class Stub(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(
                    {"command": {"app": "Safari", "id": 3,
                                 "ts": time.time()}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            cmd = BRIDGE.fetch_focus("http://127.0.0.1:%d" % port)
        finally:
            server.shutdown()
            thread.join()
        self.assertEqual(cmd["app"], "Safari")
        self.assertIsNone(BRIDGE.fetch_focus("http://127.0.0.1:9"))


class TalonSideTest(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(TALON_SIDE.clean_name("Safari"), "Safari")
        self.assertEqual(TALON_SIDE.clean_name(" \x00a\x07 "), "a")
        self.assertEqual(len(TALON_SIDE.clean_name("z" * 200)),
                         TALON_SIDE.NAME_CHARS)

    def test_build_state_doc_bounded_deduped_sorted(self):
        doc = TALON_SIDE.build_state_doc(
            ["Safari", "Safari", "", "Mail"], "Safari")
        self.assertEqual(doc["apps"], ["Mail", "Safari"])
        self.assertEqual(doc["focused"], "Safari")
        doc = TALON_SIDE.build_state_doc(
            ["App%d" % i for i in range(100)], "")
        self.assertEqual(len(doc["apps"]), TALON_SIDE.MAX_APPS)

    def test_handle_focus_doc_refuses_without_calling(self):
        calls = []
        stale = {"id": 1, "name": "Safari",
                 "ts": time.time() - 60}
        resp = TALON_SIDE.handle_focus_doc(stale, calls.append)
        self.assertFalse(resp["ok"])
        self.assertEqual(calls, [])
        empty = {"id": 2, "name": "  ", "ts": time.time()}
        resp = TALON_SIDE.handle_focus_doc(empty, calls.append)
        self.assertFalse(resp["ok"])
        self.assertEqual(calls, [])

    def test_handle_focus_doc_focuses_and_reports(self):
        calls = []
        req = {"id": 3, "name": "Safari", "ts": time.time()}
        resp = TALON_SIDE.handle_focus_doc(req, calls.append)
        self.assertEqual(calls, ["Safari"])
        self.assertEqual(resp, {"id": 3, "ok": True,
                                "focused": "Safari"})

    def test_capture_doc_captures_rect_to_fixed_path(self):
        if not hasattr(TALON_SIDE, "handle_capture_doc"):
            self.skipTest("talon side predates the capture verb")
        made = {}

        def rect_of(x, y, w, h):
            made["rect"] = (x, y, w, h)
            return made["rect"]

        def shoot(rect, path):
            made["shot"] = (rect, path)
        req = {"id": 9, "x": 10, "y": 20, "w": 480, "h": 360,
               "ts": time.time()}
        self.assertEqual(TALON_SIDE.handle_capture_doc(
            req, rect_of, shoot, "/tmp/fixed.png"),
            {"id": 9, "ok": True})
        self.assertEqual(made["rect"], (10.0, 20.0, 480.0, 360.0))
        self.assertEqual(made["shot"],
                         (made["rect"], "/tmp/fixed.png"))

    def test_capture_doc_refuses_without_capturing(self):
        if not hasattr(TALON_SIDE, "handle_capture_doc"):
            self.skipTest("talon side predates the capture verb")
        calls = []

        def shoot(rect, path):
            calls.append((rect, path))

        stale = {"id": 1, "x": 0, "y": 0, "w": 10, "h": 10,
                 "ts": time.time() - 60}
        resp = TALON_SIDE.handle_capture_doc(stale, None, shoot,
                                             "/tmp/fixed.png")
        self.assertFalse(resp["ok"])
        for bad in ({"id": 2, "x": 0, "y": 0, "w": 99999,
                     "h": 10, "ts": time.time()},
                    {"id": 3, "ts": time.time()},
                    "junk"):
            resp = TALON_SIDE.handle_capture_doc(bad, None, shoot,
                                                 "/tmp/fixed.png")
            self.assertFalse(resp["ok"])
        self.assertEqual(calls, [])

    def test_no_blocking_rpc_import(self):
        # The Talon side must not use the command_client blocking
        # primitive (brain-15l95): no rpc_client call, no main-thread
        # sleep loop, no keystroke trigger anywhere in the module.
        # (Prose mentions in the docstring are fine; calls are not.)
        import inspect
        src = inspect.getsource(TALON_SIDE)
        for banned in ("rpc_client_", "read_json_with_timeout(",
                       "trigger_command_server_command_execution",
                       "actions.sleep(", "actions.key("):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
