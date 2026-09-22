"""Stream renderer (jumbotron live monitor) tests.

Headless throughout: Screen over HeadlessFramebuffer, so the decode +
resize + present path under test is the real one, minus photons.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import base64
import io
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from displayd import FeedStore, HeadlessFramebuffer, Screen, validate_value

from PIL import Image


def make_screen(w=320, h=180):
    screen = Screen(HeadlessFramebuffer(width=w, height=h))
    screen.feeds = FeedStore()
    return screen


def jpeg_bytes(w=320, h=180, color=(200, 30, 30)):
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def stream_mod():
    found = displayd.load_renderers(displayd.RENDERER_DIR)
    assert "stream" in found, "stream renderer failed to load: %s" % found.get("stream")
    return found["stream"]["module"]


class TestStreamAdvertised(unittest.TestCase):
    def test_loads_and_loops(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("stream", found)
        self.assertIn("module", found["stream"])
        self.assertFalse(found["stream"]["static"])
        self.assertIn("frame", found["stream"]["inputs"])

    def test_frame_input_keeps_latest_only(self):
        spec = stream_mod().INPUTS["frame"]
        self.assertEqual(spec["buffer"], 1)

    def test_fps_clamp(self):
        mod = stream_mod()
        self.assertEqual(mod._clamp_fps(None), 2.0)
        self.assertEqual(mod._clamp_fps(60), 5.0)
        self.assertEqual(mod._clamp_fps(0.01), 0.5)
        self.assertEqual(mod._clamp_fps("fast"), 2.0)
        self.assertEqual(mod._clamp_fps(3), 3.0)


class TestStreamValidation(unittest.TestCase):
    def test_frame_payload_validates(self):
        spec = stream_mod().INPUTS["frame"]
        validate_value({"data": base64.b64encode(jpeg_bytes()).decode()}, spec,
                       "stream.frame")
        validate_value({"url": "http://mac:8080/snapshot.jpg"}, spec, "stream.frame")

    def test_frame_payload_rejects_junk(self):
        spec = stream_mod().INPUTS["frame"]
        with self.assertRaises(ValueError):
            validate_value({"data": 123}, spec, "stream.frame")
        with self.assertRaises(ValueError):
            validate_value(["not", "an", "object"], spec, "stream.frame")

    def test_feed_store_keeps_latest_frame(self):
        store = FeedStore()
        spec = stream_mod().INPUTS["frame"]
        for i in range(5):
            store.push("stream", "frame", {"data": "frame-%d" % i}, spec)
        self.assertEqual(len(store.get("stream", "frame")), 1)
        self.assertEqual(store.get("stream", "frame")[-1]["data"], "frame-4")


class TestStreamRun(unittest.TestCase):
    def test_push_mode_presents_pushed_frame(self):
        mod = stream_mod()
        screen = make_screen()
        payload = {"data": base64.b64encode(jpeg_bytes()).decode()}
        screen.feeds.push("stream", "frame", payload, mod.INPUTS["frame"])
        stop = threading.Event()
        thread = threading.Thread(target=mod.run,
                                  args=(screen, {"fps": 5}, stop), daemon=True)
        thread.start()
        deadline = time.time() + 5
        while screen.fb.last_frame is None and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        thread.join(timeout=5)
        self.assertIsNotNone(screen.fb.last_frame,
                             "pushed frame never reached the framebuffer")
        # 320x180x4 BGRX bytes on the fake framebuffer.
        self.assertEqual(len(screen.fb.last_frame), 320 * 180 * 4)

    def test_idle_without_source(self):
        mod = stream_mod()
        screen = make_screen()
        stop = threading.Event()
        thread = threading.Thread(target=mod.run,
                                  args=(screen, {}, stop), daemon=True)
        thread.start()
        time.sleep(0.6)
        stop.set()
        thread.join(timeout=5)
        self.assertIsNotNone(screen.fb.last_frame, "no idle frame drawn")

    def test_bad_snapshot_url_refuses_to_run(self):
        mod = stream_mod()
        screen = make_screen()
        stop = threading.Event()
        mod.run(screen, {"url": "/etc/passwd"}, stop)
        self.assertIsNotNone(screen.fb.last_frame)

    def test_corrupt_frame_keeps_panel_alive(self):
        mod = stream_mod()
        screen = make_screen()
        screen.feeds.push("stream", "frame", {"data": "!!!not-base64!!!"},
                          mod.INPUTS["frame"])
        stop = threading.Event()
        thread = threading.Thread(target=mod.run,
                                  args=(screen, {"fps": 5}, stop), daemon=True)
        thread.start()
        time.sleep(1.0)
        stop.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "renderer thread died on bad frame")

    def test_poll_mode_against_local_snapshot_server(self):
        mod = stream_mod()
        frame = jpeg_bytes()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = frame
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            screen = make_screen()
            stop = threading.Event()
            url = "http://127.0.0.1:%d/snapshot.jpg" % server.server_port
            runner = threading.Thread(target=mod.run,
                                      args=(screen, {"url": url, "fps": 5}, stop),
                                      daemon=True)
            runner.start()
            deadline = time.time() + 5
            while screen.fb.last_frame is None and time.time() < deadline:
                time.sleep(0.05)
            stop.set()
            runner.join(timeout=5)
            self.assertIsNotNone(screen.fb.last_frame,
                                 "polled snapshot never reached the framebuffer")
        finally:
            server.shutdown()
            server.server_close()


class TestStreamThroughput(unittest.TestCase):
    def test_push_throughput_at_full_panel_size(self):
        """Measured ceiling, headless: distinct pushed frames consumed as
        fast as the pipeline sustains, 1920x1080, fps cap 5. Prints the
        achieved rate; asserts the pipeline sustains at least 1 fps."""
        mod = stream_mod()
        screen = Screen(HeadlessFramebuffer(width=1920, height=1080))
        screen.feeds = FeedStore()
        presents = []

        orig_present = screen.present

        def counting_present(img):
            presents.append(time.time())
            return orig_present(img)

        screen.present = counting_present
        stop = threading.Event()
        runner = threading.Thread(target=mod.run,
                                  args=(screen, {"fps": 5}, stop), daemon=True)
        runner.start()
        # 7-colour cycle pushed ~6.7x per tick: the sampled colour almost
        # always differs tick to tick (a divisor cycle would alias with
        # the 200ms tick and under-count presents).
        colors = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30),
                  (200, 30, 200), (30, 200, 200), (120, 120, 120)]
        end = time.time() + 6
        i = 0
        try:
            while time.time() < end:
                payload = {"data": base64.b64encode(
                    jpeg_bytes(640, 360, colors[i % len(colors)])).decode()}
                screen.feeds.push("stream", "frame", payload, mod.INPUTS["frame"])
                i += 1
                time.sleep(0.03)
        finally:
            stop.set()
            runner.join(timeout=5)
        span = presents[-1] - presents[0] if len(presents) >= 2 else 0
        rate = (len(presents) - 1) / span if span > 0 else 0.0
        print("\nstream headless ceiling: %d presents in %.1fs = %.2f fps "
              "(cap 5 fps, 1920x1080)" % (len(presents), span, rate))
        self.assertGreaterEqual(rate, 4.0,
                                "pipeline misses its 5 fps cap headless: %.2f" % rate)


if __name__ == "__main__":
    unittest.main()
