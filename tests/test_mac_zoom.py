"""Magnified-tap bridge helper tests (bridges/mac_zoom.py).

Crop math, capture bounds, click validation, and the click-fetch
shape. No real screenshots on CI (runner injected); no real clicks
anywhere (do_click is only range-checked, never fired).

Run from the repo root:  python3 -m unittest tests.test_mac_zoom -v
"""

import base64
import json
import os
import sys
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))

import mac_zoom as mz

DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": -355, "y": -1080, "w": 1920, "h": 1080},
             "main": False}]


class CropTest(unittest.TestCase):
    def test_centered_and_clamped(self):
        crop = mz.crop_for(100, 100, DISPLAYS)
        self.assertEqual(crop, {"x": 0, "y": 0, "w": 480,
                                "h": 360})
        # Corner clamps inside the display, never outside it.
        edge = mz.crop_for(10, 10, DISPLAYS)
        self.assertGreaterEqual(edge["x"], 0)
        self.assertGreaterEqual(edge["y"], 0)
        far = mz.crop_for(1700, 1100, DISPLAYS)
        self.assertLessEqual(far["x"] + far["w"], 1728)
        self.assertLessEqual(far["y"] + far["h"], 1117)

    def test_second_display(self):
        crop = mz.crop_for(464, -283, DISPLAYS)
        self.assertIsNotNone(crop)
        self.assertGreaterEqual(crop["x"], -355)

    def test_off_all_displays_is_none(self):
        self.assertIsNone(mz.crop_for(9000, 9000, DISPLAYS))
        self.assertIsNone(mz.crop_for(100, 100, []))
        self.assertIsNone(mz.crop_for("x", 100, DISPLAYS))
        self.assertIsNone(mz.crop_for(100, 100, None))


class CaptureTest(unittest.TestCase):
    def test_capture_reads_bounded_bytes(self):
        blob = b"\xff\xd8" + b"x" * 1000

        def runner(cmd, timeout=None, capture_output=None):
            path = cmd[-1]
            with open(path, "wb") as fh:
                fh.write(blob)

            class Proc:
                returncode = 0
                stderr = b""
            return Proc()

        self.assertEqual(mz.capture({"x": 0, "y": 0, "w": 480,
                                     "h": 360}, runner=runner), blob)
        self.assertEqual(mz.encode(blob), base64.b64encode(blob).decode())

    def test_capture_failures_raise(self):
        def failing(cmd, timeout=None, capture_output=None):
            class Proc:
                returncode = 1
                stderr = b"denied"
            return Proc()

        with self.assertRaises(RuntimeError):
            mz.capture({"x": 0, "y": 0, "w": 480, "h": 360},
                       runner=failing)
        with self.assertRaises(ValueError):
            mz.capture({"x": 0, "y": 0, "w": 99999, "h": 99999},
                       runner=failing)

    def test_encode_caps_size(self):
        self.assertIsNone(mz.encode(b"x" * (mz.JPEG_CAP + 1)))
        self.assertIsNone(mz.encode(b""))
        self.assertIsNone(mz.encode(None))

    def test_wire_bound_holds_schema_ceiling(self):
        # Worst case: a full-cap file must still pass the daemon's
        # maxLength gate (140000), or the bound is a lie.
        import base64
        text = base64.b64encode(b"x" * mz.JPEG_CAP).decode()
        self.assertLessEqual(len(text), 140000)

    def test_zoom_doc(self):
        doc = mz.zoom_doc(1.5, 2.5, "abc", 100.0)
        self.assertEqual(doc, {"ts": 100.0, "x": 1.5, "y": 2.5,
                               "jpeg": "abc"})
        self.assertIsNone(mz.zoom_doc(1, 2, "", 100.0))
        self.assertIsNone(mz.zoom_doc("x", 2, "abc", 100.0))


class ClickTest(unittest.TestCase):
    def test_range_checked_without_quartz(self):
        # Never fires a click here: out-of-range raises before any OS
        # call, and a real click is only exercised live by hand.
        with self.assertRaises(ValueError):
            mz.do_click(200000, 0)
        with self.assertRaises(ValueError):
            mz.do_click("x", 0)

    def test_fetch_click_command_shape(self):
        now = time.time()

        class Stub(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(
                    {"command": {"x": 100, "y": 200,
                                 "display_index": 0,
                                 "ts": now}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            cmd = mz.fetch_click_command(
                "http://127.0.0.1:%d" % server.server_address[1])
        finally:
            server.shutdown()
            thread.join()
        self.assertEqual(cmd["x"], 100)
        self.assertIsNone(
            mz.fetch_click_command("http://127.0.0.1:9"))

    def test_stale_click_never_fetches(self):
        class Old(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(
                    {"command": {"x": 1, "y": 2,
                                 "ts": time.time() - 9999}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Old)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            self.assertIsNone(mz.fetch_click_command(
                "http://127.0.0.1:%d" % server.server_address[1]))
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    unittest.main()
