"""Tap-to-click through the REAL stack with LIVE feeds (task-ax59w).

Marries MacbookTwoTapTest (tests/test_touch.py) to live feeds: a real
headless DisplayDaemon over HTTP, the real touch DisplaydClient, and
the real TouchService dispatcher. Tap 1 (GLANCE map) warps + enters
AIM; with a FRESH zoom feed tap 2 (AIM review) returns 200 and queues
a click command. With a STALE zoom feed (the captain's 490 s-old
capture vs CLICK_FRESH 30 s) tap 2 is 409-refused and queues nothing:
that refusal IS the reported bug, pinned here so the fixed capture
path (bridges/mac_zoom.py ffmpeg fallback) is what keeps it green in
production.

Run from the repo root:  python3 -m unittest tests.test_macbook_tap_live -v
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
                                "renderers"))

import displayd
import macbook_layout as lay
import macbook_map
import touch
from touch import DisplaydClient, TouchEvent, TouchService, default_config

DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": -355, "y": -1080, "w": 1920, "h": 1080},
             "main": False}]
QX, QY = 464.0, -283.0  # a point on the second display
PANEL_W, PANEL_H = 1920, 1080
STALE_AGE = 490.0  # the captain's zoom-feed age at diagnosis (task-ax59w)


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
        box, PANEL_W, PANEL_H, top=lay.header_bottom(),
        bottom=PANEL_H)
    px, py = macbook_map.project(qx, qy, scale, ox, oy)
    return int(round(px)), int(round(py))


class LiveTapTest(unittest.TestCase):
    """End-to-end taps against a live headless daemon + live feeds."""

    @classmethod
    def setUpClass(cls):
        cls._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(cls.tmp.name, "policy.json"),
            feedback_path=os.path.join(cls.tmp.name, "feedback.jsonl"))
        cls.old_daemon = displayd.DAEMON
        displayd.DAEMON = cls.daemon
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         displayd.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.client = DisplaydClient(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        displayd.DAEMON = cls.old_daemon
        cls.tmp.cleanup()
        if cls._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = cls._env

    def setUp(self):
        self.daemon.clear()
        self.daemon.click_pending = None
        self.daemon.mouse_pending = None
        cfg = default_config()
        cfg.update({
            "width": PANEL_W, "height": PANEL_H,
            "calibration": {"x_min": 0, "x_max": PANEL_W - 1,
                            "y_min": 0, "y_max": PANEL_H - 1},
            "tap_max_seconds": 60, "debounce_seconds": 0,
            "regions": [],
            "tap_options": {"enabled": False},
            "view_regions": {
                "macbook": [{"id": e["id"],
                             "rect": list(e["rect"]),
                             "action": dict(e["action"])}
                            for e in lay.touch_regions(PANEL_W, PANEL_H)]},
            "endpoint": self.base,
        })
        self.svc = TouchService(cfg, client=self.client)
        ack = self.svc.reannounce()
        self.assertTrue((ack or {}).get("ok"), ack)

    # -- helpers ------------------------------------------------------

    def _tap(self, x, y):
        self.svc.handle_frame([TouchEvent("down", 0, x, y)])
        return self.svc.handle_frame([TouchEvent("up", 0, x, y)])

    def _get(self, path):
        with urllib.request.urlopen(
                self.base + path, timeout=5) as resp:
            return resp.status, json.loads(
                resp.read().decode("utf-8", "replace") or "{}")

    def _post(self, path, body):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(
                    resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(
                exc.read().decode("utf-8", "replace") or "{}")

    def _glance_with_fresh_state(self):
        status, _ = self._post("/show", {"renderer": "macbook"})
        self.assertEqual(status, 200)
        status, _ = self._post("/feed/macbook/state", state_payload())
        self.assertEqual(status, 200)

    def _tap1_enters_aim(self):
        """Positioning tap on the GLANCE map: warp queued + AIM entered."""
        self._glance_with_fresh_state()
        first = self._tap(*panel_of(QX, QY))
        self.assertEqual(first.get("path"), "/macbook/mouse", first)
        self.assertEqual(first.get("status"), 200, first)
        status, doc = self._get("/state")
        self.assertEqual(status, 200)
        self.assertEqual(doc.get("renderer"), "macbook")
        self.assertEqual((doc.get("params") or {}).get("mode"), "aim")
        return first

    # -- the regression -------------------------------------------------

    def test_safeties_frozen(self):
        # Anti-blind-click gates retune only with captain approval:
        # pin them here so no worker judgment drifts them.
        self.assertEqual(displayd.DisplayDaemon.CLICK_FRESH, 30.0)
        self.assertEqual(displayd.DisplayDaemon.CLICK_EPS, 8.0)

    def test_tap2_with_live_feeds_queues_click(self):
        # The fixed capture path posts a fresh review after the warp;
        # the image tap then clicks the reviewed point, never a blind
        # one. Routing is mode-aware (resolve names the catcher) and
        # the commit gates pass on live feeds.
        self._tap1_enters_aim()
        status, _ = self._post("/feed/macbook/zoom", zoom_payload())
        self.assertEqual(status, 200)
        status, resolved = self._post("/touch/resolve",
                                      {"x": 960, "y": 540})
        self.assertEqual(status, 200, resolved)
        self.assertEqual(resolved.get("region"), "mac-zoom", resolved)
        self.assertFalse(resolved.get("refused"), resolved)
        second = self._tap(960, 540)
        self.assertEqual(second.get("path"), "/macbook/click", second)
        self.assertEqual(second.get("status"), 200, second)
        command = (second.get("response") or {}).get("command")
        self.assertIsNotNone(command, second)
        self.assertAlmostEqual(command["x"], QX, delta=5)
        self.assertAlmostEqual(command["y"], QY, delta=5)
        # The Mac-side poller mirror sees the same queued command.
        status, doc = self._get("/macbook/click?since=0")
        self.assertEqual(status, 200, doc)
        self.assertEqual((doc.get("command") or {}).get("id"),
                         command["id"], doc)

    def test_tap2_with_stale_zoom_refused(self):
        # The captain's symptom, pinned: a 490 s-old review capture
        # (CLICK_FRESH 30 s) refuses the image tap with the fresh-
        # capture reason and queues NOTHING. Routing still names the
        # catcher -- the refusal is the feed gate, not dispatch.
        self._tap1_enters_aim()
        status, _ = self._post(
            "/feed/macbook/zoom",
            zoom_payload(ts=time.time() - STALE_AGE))
        self.assertEqual(status, 200)
        status, resolved = self._post("/touch/resolve",
                                      {"x": 960, "y": 540})
        self.assertEqual(status, 200, resolved)
        self.assertEqual(resolved.get("region"), "mac-zoom", resolved)
        self.assertFalse(resolved.get("refused"), resolved)
        second = self._tap(960, 540)
        self.assertEqual(second.get("action"), "macbook_click", second)
        err = second.get("error", "")
        self.assertIn("409", err, second)
        self.assertIn("review", err, second)
        self.assertIsNone(
            getattr(self.daemon, "click_pending", None),
            "a refused tap must queue no click command")


if __name__ == "__main__":
    unittest.main()
