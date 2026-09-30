"""Command-endpoint long-poll (?wait=) tests.

The tap-latency fast lane: GET /talon/focus, /macbook/mouse and
/macbook/click accept an optional ?wait= hold that wakes the instant a
tap queues instead of waiting for the bridge's next poll tick. The TTL
slot stays the truth; wait=0 (or absent) is byte-identical to today.

Run from the repo root:  python3 -m unittest tests.test_command_longpoll -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd  # noqa: E402
import macbook_layout  # noqa: E402
import macbook_map  # noqa: E402

DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": -355, "y": -1080, "w": 1920, "h": 1080},
             "main": False}]
PANEL_W, PANEL_H = 1920, 1080
QX, QY = 464.0, -283.0


def state_payload(ts=None, mouse=None):
    return {
        "ts": ts if ts is not None else time.time(),
        "accessibility_trusted": True,
        "focus": {"app_name": "WezTerm",
                  "window_title": "t",
                  "window_bounds": {"x": 0, "y": -1049,
                                    "w": 1920, "h": 1049},
                  "display_index": 1},
        "mouse": {"x": QX if mouse is None else mouse[0],
                  "y": QY if mouse is None else mouse[1],
                  "display_index": 1},
        "displays": [{"bounds": dict(d["bounds"]), "main": d["main"]}
                     for d in DISPLAYS],
        "talon": {"mode": "command", "muted": False},
    }


def zoom_payload(ts=None, x=QX, y=QY):
    return {"ts": ts if ts is not None else time.time(),
            "x": x, "y": y, "jpeg": "AAA"}


def panel_of(qx, qy):
    box = macbook_map.union(DISPLAYS)
    scale, ox, oy = macbook_map.frame(
        box, PANEL_W, PANEL_H, top=macbook_layout.header_bottom(),
        bottom=PANEL_H)
    px, py = macbook_map.project(qx, qy, scale, ox, oy)
    return int(round(px)), int(round(py))


def chip(pos, w=1920):
    x, y, cw, ch = macbook_layout.chip_rect(pos, w)
    return int(x + cw / 2), int(y + ch / 2)


class LongpollCase(unittest.TestCase):
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
        displayd.DAEMON = self.daemon
        self.addCleanup(setattr, displayd, "DAEMON", None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          displayd.Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def get(self, path, timeout=10.0):
        t0 = time.monotonic()
        with urllib.request.urlopen(self.base + path,
                                    timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
        return (time.monotonic() - t0), doc

    def _show_glance(self):
        self.daemon.show("macbook", {})
        self.daemon.feed("macbook", "state", state_payload())
        self.daemon.feed("talon_apps", "state",
                         {"ts": time.time(),
                          "apps": ["Safari", "Terminal", "Mail"],
                          "focused": "Safari"})

    def _wake_test(self, path, queue):
        # A tap queued mid-hold wakes the GET at once -- milliseconds,
        # not the next poll tick.
        out = {}
        def _fetch():
            dt, doc = self.get("%s?since=0&wait=5" % path, timeout=10.0)
            out["dt"], out["doc"] = dt, doc
        th = threading.Thread(target=_fetch, daemon=True)
        th.start()
        time.sleep(0.2)  # let the hold park server-side
        cmd = queue()
        th.join(timeout=10.0)
        self.assertFalse(th.is_alive(), "long-poll GET never returned")
        self.assertIn("doc", out)
        self.assertEqual(out["doc"]["command"]["id"], cmd["id"])
        self.assertLess(out["dt"], 1.0,
                        "tap did not wake the hold promptly: %.3fs"
                        % out["dt"])

    def test_idle_no_wait_is_immediate_none(self):
        for path in ("/talon/focus", "/macbook/mouse", "/macbook/click"):
            dt, doc = self.get("%s?since=0" % path)
            self.assertEqual(doc, {"command": None})
            self.assertLess(dt, 1.0, "%s slow without wait" % path)

    def test_garbage_wait_degrades_to_today(self):
        for wait in ("abc", "-2", "", "NaN"):
            dt, doc = self.get("/talon/focus?since=0&wait=%s" % wait,
                               timeout=5.0)
            self.assertEqual(doc, {"command": None})
            self.assertLess(dt, 1.0, "wait=%r did not degrade" % wait)

    def test_idle_hold_is_bounded(self):
        # Nothing queues: the hold expires on its own (~0.3s), never hung.
        dt, doc = self.get("/talon/focus?since=0&wait=0.3", timeout=5.0)
        self.assertEqual(doc, {"command": None})
        self.assertGreaterEqual(dt, 0.25)
        self.assertLess(dt, 2.0)

    def test_wait_clamps_to_max(self):
        self.daemon.CMD_WAIT_MAX = 0.2
        dt, doc = self.get("/talon/focus?since=0&wait=60", timeout=5.0)
        self.assertEqual(doc, {"command": None})
        self.assertLess(dt, 2.0, "wait=60 escaped the clamp: %.3fs" % dt)

    def test_focus_tap_wakes_hold(self):
        self._show_glance()
        def _queue():
            result = self.daemon.request_focus_move(*chip(0))
            self.assertTrue(result["ok"], result)
            return result["command"]
        self._wake_test("/talon/focus", _queue)

    def test_mouse_tap_wakes_hold(self):
        self._show_glance()
        def _queue():
            result = self.daemon.request_mouse_move(*panel_of(QX, QY))
            self.assertTrue(result["ok"], result)
            return result["command"]
        self._wake_test("/macbook/mouse", _queue)

    def test_click_tap_wakes_hold(self):
        # Stage 1 first (GLANCE map tap re-pins to AIM), then the review
        # capture of that point: the click slot fires on the review only.
        self._show_glance()
        moved = self.daemon.request_mouse_move(*panel_of(QX, QY))
        self.assertTrue(moved["ok"], moved)
        self.daemon.feed("macbook", "zoom", zoom_payload())
        def _queue():
            result = self.daemon.request_click_move(*panel_of(QX, QY))
            self.assertTrue(result["ok"], result)
            return result["command"]
        self._wake_test("/macbook/click", _queue)

    def test_seen_command_not_redelivered_while_waiting(self):
        self._show_glance()
        result = self.daemon.request_focus_move(*chip(1))
        self.assertTrue(result["ok"], result)
        ts = result["command"]["ts"]
        dt, doc = self.get("/talon/focus?since=%s&wait=0.5" % ts,
                           timeout=5.0)
        self.assertEqual(doc, {"command": None})
        self.assertGreaterEqual(dt, 0.4)

    def test_expired_command_never_fires_while_waiting(self):
        self._show_glance()
        result = self.daemon.request_focus_move(*chip(1))
        self.assertTrue(result["ok"], result)
        self.daemon.focus_pending["ts"] = time.time() - 60
        dt, doc = self.get("/talon/focus?since=0&wait=0.3", timeout=5.0)
        self.assertEqual(doc, {"command": None})
        self.assertGreaterEqual(dt, 0.25)

    def test_newer_command_wakes_despite_old_since(self):
        # since= older than the queued tap: the fast path answers at once.
        self._show_glance()
        result = self.daemon.request_focus_move(*chip(2))
        self.assertTrue(result["ok"], result)
        dt, doc = self.get("/talon/focus?since=0&wait=5", timeout=10.0)
        self.assertEqual(doc["command"]["id"], result["command"]["id"])
        self.assertLess(dt, 1.0)


def load_bridge(name):
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), os.pardir,
                        "bridges", name + ".py")
    spec = importlib.util.spec_from_file_location(
        "test_longpoll_bridge_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BridgeWaitParamTest(unittest.TestCase):
    def _stub(self, seen):
        class Stub(object):
            pass

        from http.server import BaseHTTPRequestHandler

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = json.dumps({"command": None}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        return Handler

    def _serve(self, seen):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._stub(seen))
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def test_focus_wait_param(self):
        talon_apps = load_bridge("talon_apps")
        seen = []
        base = self._serve(seen)
        self.assertIsNone(talon_apps.fetch_focus(base))
        self.assertNotIn("wait", seen[-1])
        self.assertIsNone(talon_apps.fetch_focus(base, wait=1.5))
        self.assertIn("wait=1.5", seen[-1])

    def test_mouse_wait_param(self):
        macos_state = load_bridge("macos_state")
        seen = []
        base = self._serve(seen)
        self.assertIsNone(macos_state.fetch_mouse_command(base))
        self.assertNotIn("wait", seen[-1])
        self.assertIsNone(macos_state.fetch_mouse_command(base, wait=0.5))
        self.assertIn("wait=0.5", seen[-1])

    def test_click_wait_param(self):
        mac_zoom = load_bridge("mac_zoom")
        seen = []
        base = self._serve(seen)
        self.assertIsNone(mac_zoom.fetch_click_command(base))
        self.assertNotIn("wait", seen[-1])
        self.assertIsNone(mac_zoom.fetch_click_command(base, wait=0.5))
        self.assertIn("wait=0.5", seen[-1])

    def test_talon_tick_passes_wait_through(self):
        talon_apps = load_bridge("talon_apps")
        captured = []
        real = talon_apps.fetch_focus
        talon_apps.fetch_focus = (
            lambda base, since=0.0, wait=0.0: captured.append(wait) or None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                talon_apps.tick("http://127.0.0.1:9", tmp,
                                [None, 0.0, 0.0], wait=0.5)
        finally:
            talon_apps.fetch_focus = real
        self.assertEqual(captured, [0.5])


if __name__ == "__main__":
    unittest.main()
