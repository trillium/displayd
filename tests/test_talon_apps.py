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
    def test_renderer_loads_with_inputs(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("talon_apps", found)
        entry = found["talon_apps"]
        self.assertIn("module", entry)
        self.assertIn("state", entry["inputs"])

    def test_clean_bounds_untrusted_names(self):
        self.assertEqual(RENDERER.clean("Safari"), "Safari")
        self.assertEqual(RENDERER.clean("  \x00\x07evil\x1b  "), "evil")
        self.assertEqual(RENDERER.clean(None), "")
        self.assertEqual(RENDERER.clean(123), "")
        long_name = "x" * 200
        self.assertEqual(len(RENDERER.clean(long_name)),
                         RENDERER.NAME_CHARS)

    def test_hit_rows_and_misses(self):
        w, h = 1920, 1080
        self.assertEqual(RENDERER.hit(100, RENDERER.LIST_TOP + 5,
                                     w, h, 3), 0)
        self.assertEqual(RENDERER.hit(100, RENDERER.LIST_TOP
                                     + RENDERER.ROW_H + 5, w, h, 3), 1)
        self.assertEqual(RENDERER.hit(100, RENDERER.LIST_TOP
                                     + 9 * RENDERER.ROW_H + 5, w, h, 12), 9)
        self.assertIsNone(RENDERER.hit(100, RENDERER.LIST_TOP - 5,
                                       w, h, 3))  # header
        self.assertIsNone(RENDERER.hit(100, RENDERER.LIST_TOP
                                       + 3 * RENDERER.ROW_H + 5,
                                       w, h, 3))  # past count
        self.assertIsNone(RENDERER.hit(10, RENDERER.LIST_TOP + 5,
                                       w, h, 3))  # left of PAD
        self.assertIsNone(RENDERER.hit(100, RENDERER.LIST_TOP + 5,
                                       w, h, 0))  # empty list
        self.assertIsNone(RENDERER.hit(100, RENDERER.LIST_TOP
                                       + 10 * RENDERER.ROW_H + 5,
                                       w, h, 30))  # past MAX_ROWS cap

    def test_draw_and_hit_cannot_drift(self):
        # Every drawable row hit-tests to itself on a real Screen size.
        w, h = 1920, 1080
        for i in range(RENDERER.MAX_ROWS):
            x, y, rw, rh = RENDERER.row_rect(i, w)
            self.assertEqual(RENDERER.hit(x + 5, y + 5, w, h,
                                          RENDERER.MAX_ROWS), i)


class FocusSlotTest(DaemonCase):
    def test_tap_queues_feed_named_command(self):
        daemon = self.make_daemon()
        daemon.show("talon_apps", {})
        self.feed_apps(daemon)
        y = RENDERER.LIST_TOP + 2 * RENDERER.ROW_H + 5
        result = daemon.request_focus_move(200, y)
        self.assertTrue(result["ok"])
        self.assertEqual(result["command"]["app"], "Mail")
        self.assertEqual(result["command"]["index"], 2)

    def test_name_comes_from_feed_never_caller(self):
        # The body carries pixels only: there is no parameter that could
        # smuggle a name, so a tap can only select a listed app.
        daemon = self.make_daemon()
        daemon.show("talon_apps", {})
        self.feed_apps(daemon, apps=["Safari"])
        import inspect
        params = inspect.signature(
            daemon.request_focus_move).parameters
        self.assertEqual(list(params), ["px", "py"])

    def test_view_gated(self):
        daemon = self.make_daemon()
        daemon.show("clock", {})
        self.feed_apps(daemon)
        result = daemon.request_focus_move(
            200, RENDERER.LIST_TOP + 5)
        self.assertFalse(result["ok"])
        self.assertIn("not showing", result["reason"])

    def test_stale_feed_refused(self):
        daemon = self.make_daemon()
        daemon.show("talon_apps", {})
        daemon.feed("talon_apps", "state",
                    {"ts": time.time() - 60, "apps": ["Safari"],
                     "focused": "Safari"})
        result = daemon.request_focus_move(
            200, RENDERER.LIST_TOP + 5)
        self.assertFalse(result["ok"])
        self.assertIn("fresh", result["reason"])

    def test_malformed_coords_raise(self):
        daemon = self.make_daemon()
        daemon.show("talon_apps", {})
        self.feed_apps(daemon)
        for bad in (("200", 300), (200.5, 300), (True, 300),
                    (-1, 300), (99999, 300)):
            with self.assertRaises(ValueError):
                daemon.request_focus_move(*bad)

    def test_take_focus_move_since_and_ttl(self):
        daemon = self.make_daemon()
        daemon.show("talon_apps", {})
        self.feed_apps(daemon)
        result = daemon.request_focus_move(
            200, RENDERER.LIST_TOP + 5)
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
            BRIDGE.fetch_focus = lambda base, since=0.0: None
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
