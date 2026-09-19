"""Feedback tests: store durability plus the additive displayd routes.

No framebuffer needed: DISPLAYD_FAKE_FB=1 selects the in-memory double, so
show/snapshot/feedback-frame capture run headless.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
import feedback as feedback_module
import hashlib


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "feedback.jsonl")

    def store(self):
        return feedback_module.FeedbackStore(path=self.path)

    def test_record_and_read_back(self):
        store = self.store()
        stored = store.record("text", 4, categories=["readability"],
                              notes="crisp at distance",
                              params={"text": "hi"}, agent="tester",
                              frame_png=b"\x89PNG-fake", frame_size=(8, 4))
        self.assertTrue(stored["id"].startswith("fb-"))
        self.assertTrue(stored["frame_present"])
        self.assertEqual(stored["frame"]["sha256"],
                         hashlib.sha256(b"\x89PNG-fake").hexdigest())
        got = store.get(stored["id"])
        self.assertEqual(got["notes"], "crisp at distance")
        self.assertEqual(got["params"], {"text": "hi"})

    def test_survives_reopen(self):
        first = self.store()
        stored = first.record("clock", 2, categories=["size"],
                              notes="too small", agent="tester")
        second = self.store()  # new instance, same file: a restart
        got = second.get(stored["id"])
        self.assertEqual(got["view"], "clock")
        self.assertEqual(got["rating"], 2)

    def test_validation(self):
        store = self.store()
        with self.assertRaises(ValueError):
            store.record("", 3)
        with self.assertRaises(ValueError):
            store.record("text", 0)
        with self.assertRaises(ValueError):
            store.record("text", 6)
        with self.assertRaises(ValueError):
            store.record("text", True)
        with self.assertRaises(ValueError):
            store.record("text", 3, categories=["legibility"])
        with self.assertRaises(KeyError):
            store.get("fb-nope")

    def test_list_filter_and_summary(self):
        store = self.store()
        store.record("text", 5, categories=["readability"], agent="a")
        store.record("text", 1, categories=["readability", "color"], agent="b")
        store.record("clock", 4, categories=["layout"], agent="a")
        texts = store.list(view="text")
        self.assertEqual(len(texts), 2)
        self.assertGreaterEqual(texts[0]["received_at"], texts[1]["received_at"])
        summary = store.summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["views"]["text"]["count"], 2)
        self.assertEqual(summary["views"]["text"]["avg_rating"], 3.0)
        self.assertEqual(summary["views"]["text"]["categories"]["readability"], 2)
        self.assertEqual(summary["views"]["clock"]["avg_rating"], 4.0)

    def test_pruned_frame_reads_back_frameless(self):
        store = self.store()
        stored = store.record("text", 3, frame_png=b"bytes", frame_size=(1, 1))
        os.remove(os.path.join(store.frames_dir, stored["frame"]["file"]))
        got = store.get(stored["id"])
        self.assertFalse(got["frame_present"])
        with self.assertRaises(IOError):
            store.frame_path(stored["id"])


class DaemonFeedbackTestCase(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def test_record_links_snapshot_frame(self):
        self.daemon.show("text", {"text": "HELLO"})
        self.assertTrue(self._wait_for_frame(),
                        "text view never presented a frame")
        stored = self.daemon.record_feedback(
            "text", 5, categories=["readability"], notes="big and crisp",
            params={"text": "HELLO"}, agent="mcp-test")
        self.assertTrue(stored["snapshot_captured"])
        self.assertTrue(stored["frame_present"])
        png = self.daemon.feedback_frame(stored["id"])
        self.assertTrue(png.startswith(b"\x89PNG"))
        snap = self.daemon.snapshot()
        self.assertEqual(png, snap)  # frame IS what the panel showed

    def _wait_for_frame(self, timeout=5.0):
        import time as _time
        end = _time.time() + timeout
        while _time.time() < end:
            if self.daemon.snapshot() is not None:
                return True
            _time.sleep(0.05)
        return False

    def test_record_before_anything_drawn(self):
        stored = self.daemon.record_feedback("clock", 3, notes="nothing yet")
        self.assertFalse(stored["snapshot_captured"])
        self.assertFalse(stored["frame_present"])

    def test_unknown_view_and_bad_rating(self):
        with self.assertRaises(KeyError):
            self.daemon.record_feedback("nope", 3)
        with self.assertRaises(ValueError):
            self.daemon.record_feedback("text", 9)

    def test_feed_status(self):
        # every declared input reads cold before any push
        for name, entry in self.daemon.renderers.items():
            if "module" not in entry:
                continue
            for input_name in (entry.get("inputs") or {}):
                status = self.daemon.feeds.status(name, input_name)
                self.assertEqual(status["health"], "cold")
                self.assertIsNone(status["latest"])
        with self.assertRaises(KeyError):
            self.daemon.feeds.status("text", "nope")
        with self.assertRaises(KeyError):
            self.daemon.feeds.status("nope", "nope")


class HttpFeedbackTestCase(unittest.TestCase):
    """End to end over real HTTP, including a daemon restart proving the
    feedback log is durable."""

    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self._old_daemon = displayd.DAEMON
        self.addCleanup(self._restore_daemon)
        self.feedback_path = os.path.join(self.tmp.name, "feedback.jsonl")
        self.start_daemon()

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _restore_daemon(self):
        displayd.DAEMON = self._old_daemon

    def start_daemon(self):
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=self.feedback_path)
        displayd.DAEMON = daemon
        server = ThreadingHTTPServer(("127.0.0.1", 0), displayd.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]
        self.daemon = daemon

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "image/" in ctype:
                    return resp.status, raw
                return resp.status, json.loads(raw.decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def _wait_for_frame(self, timeout=5.0):
        import time as _time
        end = _time.time() + timeout
        while _time.time() < end:
            code, snap = self.call("GET", "/snapshot")
            if code == 200:
                return True
            _time.sleep(0.05)
        return False

    def test_feedback_roundtrip_and_restart(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "text", "params": {"text": "WALL"}})
        self.assertEqual(code, 200)
        self.assertTrue(self._wait_for_frame(),
                        "text view never presented a frame")
        code, stored = self.call("POST", "/feedback",
                                 {"view": "text", "rating": 4,
                                  "categories": ["readability"],
                                  "notes": "readable from the door",
                                  "params": {"text": "WALL"},
                                  "agent": "http-test"})
        self.assertEqual(code, 201, stored)
        self.assertTrue(stored["frame_present"])
        entry_id = stored["id"]

        code, listed = self.call("GET", "/feedback")
        self.assertEqual(code, 200)
        self.assertEqual(listed["count"], 1)

        code, frame = self.call("GET", "/feedback/%s/frame" % entry_id)
        self.assertEqual(code, 200)
        self.assertTrue(frame.startswith(b"\x89PNG"))
        code, snap = self.call("GET", "/snapshot")
        self.assertEqual(code, 200)
        self.assertEqual(frame, snap)

        code, summary = self.call("GET", "/feedback/summary")
        self.assertEqual(code, 200)
        self.assertEqual(summary["views"]["text"]["avg_rating"], 4.0)

        # restart: brand-new daemon, same feedback file
        self.start_daemon()
        code, listed = self.call("GET", "/feedback")
        self.assertEqual(code, 200)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["feedback"][0]["notes"],
                         "readable from the door")
        code, summary = self.call("GET", "/feedback/summary")
        self.assertEqual(summary["total"], 1)

    def test_feedback_errors(self):
        code, body = self.call("POST", "/feedback",
                               {"view": "text", "rating": 42})
        self.assertEqual(code, 400)
        code, body = self.call("POST", "/feedback",
                               {"view": "nope", "rating": 3})
        self.assertEqual(code, 404)
        code, body = self.call("GET", "/feedback/fb-missing")
        self.assertEqual(code, 404)
        code, body = self.call("GET", "/feedback?view=text&limit=10")
        self.assertEqual(code, 200)

    def test_feed_status_route(self):
        code, body = self.call("GET", "/feed/text/nope")
        self.assertEqual(code, 404)
        # a declared input (if any renderer has one) reads cold
        for name, entry in self.daemon.renderers.items():
            if "module" not in entry:
                continue
            for input_name in (entry.get("inputs") or {}):
                code, body = self.call(
                    "GET", "/feed/%s/%s" % (name, input_name))
                self.assertEqual(code, 200, body)
                self.assertEqual(body["health"], "cold")
                return
        self.skipTest("no renderer with inputs installed")


if __name__ == "__main__":
    unittest.main()
