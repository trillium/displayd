"""Magnified-tap bridge helper tests (bridges/mac_zoom.py).

Crop math, capture bounds, click validation, and the click-fetch
shape. No real screenshots on CI (runner injected); no real clicks
anywhere (do_click is only range-checked, never fired).

Run from the repo root:  python3 -m unittest tests.test_mac_zoom -v
"""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))

import mac_zoom as mz
import mac_preview as mp
import mac_preview_capture as mpc

# Committed argv-recording double, so the command-line assertions do not
# depend on which ffmpeg (if any) this host has or where it lives.
FAKE_FFMPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "fake_ffmpeg.py")

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
    def _png(self, color=(40, 90, 140), size=(480, 360)):
        # Stand-in for Talon's fixed-name PNG (real Screenshot bytes
        # come from screen.capture_rect in the live Talon process).
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, "PNG")
        return buf.getvalue()

    def _talon(self, directory, png=None, ok=True, delay=0.05):
        # Fake Talon capture tick: answer the request id, drop the PNG
        # at the FIXED filename (never a channel-supplied path).
        import json as _json

        def run():
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                req = os.path.join(directory, "capture_request.json")
                if os.path.exists(req):
                    try:
                        with open(req) as fh:
                            doc = _json.load(fh)
                    except Exception:
                        doc = None
                    try:
                        os.unlink(req)
                    except Exception:
                        pass
                    if isinstance(doc, dict):
                        if png is not None:
                            with open(os.path.join(
                                    directory, mz.CAPTURE_IMAGE),
                                    "wb") as fh:
                                fh.write(png)
                        body = {"id": doc.get("id"), "ok": ok}
                        tmp = os.path.join(
                            directory, "capture_response.json.tmp")
                        with open(tmp, "w") as fh:
                            _json.dump(body, fh)
                        os.replace(tmp, os.path.join(
                            directory, "capture_response.json"))
                        return
                time.sleep(0.01)
        return threading.Thread(target=run)

    def test_capture_converts_talon_png_to_bounded_jpeg(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            worker = self._talon(tmp, self._png())
            worker.start()
            data = mz.capture(tmp, {"x": 0, "y": 0, "w": 480,
                                     "h": 360})
            worker.join(timeout=10)
            self.assertTrue(data.startswith(b"\xff\xd8"))  # JPEG
            self.assertLessEqual(len(data), mz.JPEG_CAP)
            self.assertEqual(mz.encode(data),
                             base64.b64encode(data).decode())

    def test_capture_failures_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # Talon refuses.
            worker = self._talon(tmp, self._png(), ok=False)
            worker.start()
            with self.assertRaises(RuntimeError):
                mz.capture(tmp, {"x": 0, "y": 0, "w": 480,
                                 "h": 360})
            worker.join(timeout=10)
            # Nobody home: timeout, not a hang.
            with tempfile.TemporaryDirectory() as tmp2:
                with self.assertRaises(RuntimeError):
                    mz.capture(tmp2, {"x": 0, "y": 0, "w": 480,
                                      "h": 360}, timeout=0.2)
        with self.assertRaises(ValueError):
            mz.capture(tmp, {"x": 0, "y": 0, "w": 99999,
                             "h": 99999})

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


class FfmpegFallbackTest(unittest.TestCase):
    """Review-capture ffmpeg fallback (task-ax59w: a broken Talon side
    must degrade to a coarser review, never a stale zoom feed that
    409-refuses every image tap). No real ffmpeg anywhere: both bridges
    are pointed at the committed argv-recording double
    (tests/fixtures/fake_ffmpeg.py), so the assertions are about the
    command line the bridge BUILDS and never about which ffmpeg a host
    happens to have, or where. No Quartz on CI (scales injected)."""

    SCALES = [(0, 0, 1728, 1117, 2.0),
              (-355, -1080, 1920, 1080, 2.0)]

    def setUp(self):
        # argv[0] is the resolved binary, not a flag: point both bridges
        # at the double so the shape checks below measure flags, not one
        # machine's Nix/Homebrew layout. (The two bridges really do
        # resolve argv[0] by different rules -- mac_preview walks
        # FFMPEG_CANDIDATES, mac_zoom pins the Homebrew path -- which is
        # a finding filed against the bridge, not a thing this test can
        # pin without re-hardcoding a path.)
        if not os.access(FAKE_FFMPEG, os.X_OK):
            self.skipTest("argv-recording ffmpeg double is not executable "
                          "(check the file mode): %s" % FAKE_FFMPEG)
        self.stub = FAKE_FFMPEG
        for module in (mz, mpc):
            old = module.FFMPEG_BIN
            module.FFMPEG_BIN = self.stub
            self.addCleanup(setattr, module, "FFMPEG_BIN", old)
        fd, self.arglog = tempfile.mkstemp(prefix="ffmpeg-argv")
        os.close(fd)
        self.addCleanup(os.unlink, self.arglog)

    def _jpeg(self, size=(480, 360)):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", size, (40, 90, 140)).save(buf, "JPEG",
                                                     quality=70)
        return buf.getvalue()

    def _patch(self, name, value):
        old = getattr(mz, name)
        setattr(mz, name, value)
        self.addCleanup(setattr, mz, name, old)

    def test_input_flags_track_preview(self):
        # The fallback rides the live-verified preview pixel path:
        # same binary input flags, only -vf gains the crop. If the
        # preview flags move, this fails until the fallback follows.
        zoom = mz.ffmpeg_review_cmd(
            1, {"x": 0, "y": 0, "w": 480, "h": 360})
        prev = mp.ffmpeg_cmd(1)
        self.assertEqual(zoom[0], self.stub)
        self.assertEqual(prev[0], self.stub)
        self.assertEqual(zoom[1:11], prev[1:11])
        self.assertEqual(zoom[-3:], ["-f", "mjpeg", "-"])
        self.assertEqual(zoom[zoom.index("-q:v") + 1],
                         str(mz._ffmpeg_q(mz.JPEG_QUALITY)))
        self.assertEqual(mz._ffmpeg_q(60), mp.ffmpeg_quality(60))
        vf = zoom[zoom.index("-vf") + 1]
        self.assertTrue(vf.startswith("crop="))
        self.assertIn("scale=", vf)

    def test_command_lines_execute_and_record_their_argv(self):
        # The shape claim above is a claim about what reaches the OS:
        # run both built command lines through a real exec and read back
        # the argv the process saw. Guards against an argv the shape
        # checks would still accept but exec would not.
        env = dict(os.environ, FAKE_FFMPEG_LOG=self.arglog)
        zoom = mz.ffmpeg_review_cmd(
            1, {"x": 0, "y": 0, "w": 480, "h": 360})
        prev = mp.ffmpeg_cmd(1)
        for argv in (zoom, prev):
            done = subprocess.run(argv, env=env, timeout=30,
                                  capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        with open(self.arglog, encoding="utf-8") as fh:
            recorded = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(recorded, [zoom, prev])
        self.assertEqual(recorded[0][1:11], recorded[1][1:11])

    def test_crop_snapped_even(self):
        # yuv420p refuses odd crop geometry: odd device rects snap.
        zoom = mz.ffmpeg_review_cmd(
            0, {"x": 1, "y": 3, "w": 481, "h": 361})
        vf = zoom[zoom.index("-vf") + 1]
        self.assertTrue(vf.startswith("crop=480:360:0:2,"), vf)

    def test_review_crop_posts_device_pixels(self):
        seen = {}

        class Proc:
            returncode = 0
            stdout = self._jpeg()
            stderr = b""

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return Proc()

        self._patch("display_pixel_scales", lambda: list(self.SCALES))
        self._patch("subprocess",
                    type("S", (), {"run": staticmethod(fake_run),
                                    "DEVNULL": mz.subprocess.DEVNULL,
                                    "PIPE": mz.subprocess.PIPE}))
        data = mz.ffmpeg_review_crop(100, 100, DISPLAYS)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        vf = seen["argv"][seen["argv"].index("-vf") + 1]
        # (100,100) on the 2x main display: 480x360 pt -> 960x720 dev.
        self.assertTrue(vf.startswith("crop=960:720:"), vf)

    def test_review_crop_failures_are_none(self):
        self._patch("display_pixel_scales", lambda: list(self.SCALES))

        class Bad:
            returncode = 1
            stdout = b""
            stderr = b"no grant"

        self._patch("subprocess",
                    type("S", (), {"run": staticmethod(
                        lambda argv, **kw: Bad()),
                        "DEVNULL": mz.subprocess.DEVNULL,
                        "PIPE": mz.subprocess.PIPE}))
        self.assertIsNone(mz.ffmpeg_review_crop(100, 100, DISPLAYS))
        # No scales (CI has no Quartz): no fallback, Talon unaffected.
        self._patch("display_pixel_scales", lambda: None)
        self.assertIsNone(mz.ffmpeg_review_crop(100, 100, DISPLAYS))
        # Off every display: nothing to crop.
        self._patch("display_pixel_scales", lambda: list(self.SCALES))
        self.assertIsNone(mz.ffmpeg_review_crop(9000, 9000, DISPLAYS))

    def test_position_hook_falls_back_when_talon_fails(self):
        posted = {}

        def boom(directory, crop, timeout=4.0):
            raise RuntimeError("talon capture refused: None")

        self._patch("capture", boom)
        self._patch("ffmpeg_review_crop", lambda *a, **k: self._jpeg())
        self._patch("post_zoom",
                    lambda base, doc: posted.setdefault("doc", doc)
                    or True)
        state = {"displays": DISPLAYS}
        self.assertTrue(mz.position_hook("http://x", "/tmp", state,
                                         100, 100, now=123.0))
        self.assertEqual(posted["doc"]["x"], 100.0)
        self.assertEqual(posted["doc"]["ts"], 123.0)

    def test_position_hook_false_when_both_fail(self):
        def boom(directory, crop, timeout=4.0):
            raise RuntimeError("talon down")

        self._patch("capture", boom)
        self._patch("ffmpeg_review_crop", lambda *a, **k: None)
        called = []
        self._patch("post_zoom",
                    lambda base, doc: called.append(doc) or True)
        state = {"displays": DISPLAYS}
        self.assertFalse(mz.position_hook("http://x", "/tmp", state,
                                          100, 100))
        self.assertEqual(called, [])


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
