"""Tests for the retro_grid renderer. No framebuffer or touch hardware needed.

Run from the repo root:  python3 -m pytest tests/test_retro_grid.py -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
import touch
from PIL import Image

from renderers import retro_grid as rg

W, H = 1920, 1080


class FakeScreen:
    W, H = W, H

    def __init__(self):
        self.frames = []
        self.taps = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    def clear(self, background=(0, 0, 0)):
        self.present(self.new_image(background))

    def get_input(self, renderer, name):
        assert (renderer, name) == ("retro_grid", "tap")
        return list(self.taps)

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return displayd.Screen.color(value, default)

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return displayd.Screen.font_path(family)


def geom(w=W, h=H, **kw):
    return rg.grid_geometry(w, h, **kw)


def boxes(n=12):
    return rg.coerce_boxes({}, n)


class LayoutMathTest(unittest.TestCase):
    def test_default_is_4x3(self):
        rects = geom()
        self.assertEqual(len(rects), 12)

    def test_fills_screen_with_even_gutters(self):
        g = rg.default_gutter(W, H)
        rects = geom()
        # Outer margins equal the gutter on all sides.
        self.assertEqual((rects[0][0], rects[0][1]), (g, g))
        last = rects[-1]
        self.assertEqual(last[0] + last[2] + g, W)
        self.assertEqual(last[1] + last[3] + g, H)
        # Uniform cell size; inter-cell gaps equal the gutter too.
        sizes = {(r[2], r[3]) for r in rects}
        self.assertEqual(len(sizes), 1)
        self.assertEqual(rects[1][0] - (rects[0][0] + rects[0][2]), g)
        self.assertEqual(rects[4][1] - (rects[0][1] + rects[0][3]), g)

    def test_cells_inside_bounds_no_overlap(self):
        rects = geom()
        for (x, y, w, h) in rects:
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, W)
            self.assertLessEqual(y + h, H)
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap = (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                           and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
                self.assertFalse(overlap, (a, b))

    def test_recompute_for_other_sizes(self):
        for (w, h) in ((800, 480), (1280, 720), (3840, 2160)):
            rects = geom(w, h)
            self.assertEqual(len(rects), 12)
            for (x, y, cw, ch) in rects:
                self.assertLessEqual(x + cw, w)
                self.assertLessEqual(y + ch, h)

    def test_custom_cols_rows_gutter(self):
        rects = geom(cols=2, rows=2, gutter=10)
        self.assertEqual(len(rects), 4)
        self.assertEqual((rects[0][0], rects[0][1]), (10, 10))
        self.assertEqual(rects[1][0] - (rects[0][0] + rects[0][2]), 10)


class BoxesTest(unittest.TestCase):
    def test_defaults_are_labels_1_to_12(self):
        cells = boxes()
        self.assertEqual([c["label"] for c in cells],
                         [str(i) for i in range(1, 13)])
        self.assertTrue(all(rg.center_kind(c) == "text" for c in cells))

    def test_partial_override_and_text_key(self):
        cells = rg.coerce_boxes(
            {"boxes": [{"label": "GO"}, {"text": "STOP"}, "junk",
                       {"image": "/tmp/x.png", "label": "PIC"}]}, 4)
        self.assertEqual(cells[0]["label"], "GO")
        self.assertEqual(cells[1]["label"], "STOP")
        self.assertEqual(cells[2]["label"], "3")  # garbage entry -> default
        self.assertEqual(cells[3]["label"], "PIC")
        self.assertEqual(rg.center_kind(cells[3]), "image")
        self.assertEqual(rg.center_kind(cells[0]), "text")

    def test_extra_boxes_ignored(self):
        cells = rg.coerce_boxes({"boxes": [{"label": str(i)} for i in range(99)]}, 12)
        self.assertEqual(len(cells), 12)

    def test_garbage_params_never_raise(self):
        for bad in (None, {}, {"boxes": None}, {"boxes": "nope"},
                    {"boxes": [None, 42, {"label": None}]}):
            cells = rg.coerce_boxes(bad, 3)
            self.assertEqual(len(cells), 3)


class ImageLoadTest(unittest.TestCase):
    def test_real_file_loads(self):
        img = Image.new("RGB", (32, 32), (255, 0, 0))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            path = fh.name
        try:
            img.save(path)
            loaded = rg._load_center(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.size, (32, 32))
        finally:
            os.unlink(path)

    def test_missing_file_falls_back_to_none(self):
        self.assertIsNone(rg._load_center("/nonexistent/retro-cell.png"))
        self.assertIsNone(rg._load_center(""))
        self.assertIsNone(rg._load_center(None))

    def test_url_detection(self):
        self.assertTrue(rg._is_url("https://example.com/a.png"))
        self.assertTrue(rg._is_url("http://100.81.88.113:8980/snapshot"))
        self.assertFalse(rg._is_url("/tmp/a.png"))
        self.assertFalse(rg._is_url("C:\\pics\\a.png"))


class ResolveTapTest(unittest.TestCase):
    def setUp(self):
        self.cells = boxes()
        self.rects = geom()

    def center(self, i):
        x, y, w, h = self.rects[i]
        return (x + w // 2, y + h // 2)

    def test_direct_cell_numbers(self):
        self.assertEqual(rg.resolve_tap({"cell": 5}, self.cells, self.rects, W, H), 4)
        self.assertEqual(rg.resolve_tap({"cell": 1}, self.cells, self.rects, W, H), 0)
        self.assertEqual(rg.resolve_tap({"cell": 12}, self.cells, self.rects, W, H), 11)
        self.assertIsNone(rg.resolve_tap({"cell": 13}, self.cells, self.rects, W, H))
        self.assertIsNone(rg.resolve_tap({"cell": True}, self.cells, self.rects, W, H))

    def test_label_and_region_tokens(self):
        self.assertEqual(rg.resolve_tap({"label": "7"}, self.cells, self.rects, W, H), 6)
        self.assertEqual(rg.resolve_tap({"region": "retro-cell-7"}, self.cells, self.rects, W, H), 6)
        self.assertEqual(rg.resolve_tap({"id": "retro-cell-12"}, self.cells, self.rects, W, H), 11)
        self.assertIsNone(rg.resolve_tap({"region": "nope"}, self.cells, self.rects, W, H))

    def test_coordinates_hit_test(self):
        x, y = self.center(10)
        self.assertEqual(rg.resolve_tap({"x": x, "y": y}, self.cells, self.rects, W, H), 10)
        # touch.py confidence payload shape (pixel + norms + region).
        payload = {"x": x, "y": y, "x_norm": x / W, "y_norm": y / H,
                   "region": "retro-cell-11", "action": "notify", "hit": True}
        self.assertEqual(rg.resolve_tap(payload, self.cells, self.rects, W, H), 10)
        # Gutter point between cells: dead zone.
        g = rg.default_gutter(W, H)
        x0, y0, w0, h0 = self.rects[0]
        self.assertIsNone(
            rg.resolve_tap({"x": x0 + w0 + g // 2, "y": y0 + 5},
                           self.cells, self.rects, W, H))

    def test_garbage_and_dead_zone(self):
        for bad in (None, "tap", 42, {}, {"hit": False},
                    {"x": None, "y": 5}, {"x": "left", "y": 1}):
            self.assertIsNone(rg.resolve_tap(bad, self.cells, self.rects, W, H), bad)


class HighlightTest(unittest.TestCase):
    def test_newest_tap_wins_then_expires(self):
        cells, rects = boxes(), geom()
        taps = [({"cell": 2}, 100.0), ({"cell": 9}, 101.0)]
        self.assertEqual(rg.current_highlight(taps, cells, rects, W, H, 101.5, 1.2), 8)
        self.assertIsNone(rg.current_highlight(taps, cells, rects, W, H, 103.0, 1.2))

    def test_unresolvable_taps_skipped(self):
        cells, rects = boxes(), geom()
        taps = [({"cell": 99}, 100.0), ({"cell": 3}, 100.0)]
        self.assertEqual(rg.current_highlight(taps, cells, rects, W, H, 100.5, 5.0), 2)
        self.assertIsNone(rg.current_highlight([({"cell": 99}, 100.0)],
                                               cells, rects, W, H, 100.5, 5.0))


class TouchRegionsTest(unittest.TestCase):
    def test_regions_match_default_layout(self):
        regions = rg.touch_regions()
        self.assertEqual(len(regions), 12)
        rects = geom()
        ids = set()
        for i, entry in enumerate(regions):
            self.assertEqual(entry["id"], "retro-cell-%d" % (i + 1))
            ids.add(entry["id"])
            self.assertEqual(tuple(entry["rect"]), rects[i])
        self.assertEqual(len(ids), 12)

    def test_region_actions_use_existing_allowlist(self):
        # No new generic action: every cell action must resolve through
        # the closed touch.py ACTION_TABLE today, not after a daemon change.
        for entry in rg.touch_regions():
            method, path, body = touch.action_request(entry["action"])
            self.assertEqual(method, "POST")
            self.assertTrue(path.startswith("/"))
            self.assertIn("CELL", body["title"])

    def test_shipped_example_matches_module(self):
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "touch-retro-grid.json.example")
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["width"], 1920)
        self.assertEqual(doc["height"], 1080)
        self.assertEqual(doc["confidence_feedback"],
                         {"enabled": True, "renderer": "retro_grid",
                          "input": "tap"})
        self.assertEqual(doc["regions"], rg.touch_regions())
        cfg = dict(doc)
        cfg.pop("_comment", None)
        loaded = touch.load_config(None)  # defaults still validate
        self.assertTrue(loaded["regions"])
        # The example itself validates (minus its comment key).
        import tempfile as tf
        with tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            tmp = fh.name
        try:
            touch.load_config(tmp)
        finally:
            os.unlink(tmp)


class RenderSmokeTest(unittest.TestCase):
    def fills(self, screen):
        return [screen.color(None, rg.PALETTE[i % len(rg.PALETTE)])
                for i in range(12)]

    def test_draw_produces_full_frame_and_highlight_differs(self):
        screen = FakeScreen()
        cells, rects = boxes(), geom()
        fills = self.fills(screen)
        plain = rg.draw(screen, cells, rects, fills,
                        screen.color(None, rg.DEFAULT_BORDER), None, {})
        self.assertEqual(plain.size, (W, H))
        flashed = rg.draw(screen, cells, rects, fills,
                          screen.color(None, rg.DEFAULT_BORDER), 4, {})
        self.assertEqual(flashed.size, (W, H))
        self.assertNotEqual(plain.tobytes(), flashed.tobytes())

    def test_draw_with_image_center_and_bad_image(self):
        screen = FakeScreen()
        img = Image.new("RGB", (64, 64), (0, 255, 0))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            path = fh.name
        try:
            img.save(path)
            cells = rg.coerce_boxes(
                {"boxes": [{"image": path, "label": "PIC"},
                           {"image": "/nonexistent/x.png", "label": "7"}]}, 12)
            loaded = {0: rg._load_center(cells[0]["image"])}
            self.assertIsNone(rg._load_center(cells[1]["image"]))
            frame = rg.draw(screen, cells, geom(), self.fills(screen),
                            screen.color(None, rg.DEFAULT_BORDER), None, loaded)
            self.assertEqual(frame.size, (W, H))
        finally:
            os.unlink(path)

    def test_run_flashes_on_tap_then_clears(self):
        screen = FakeScreen()
        stop = threading.Event()
        worker = threading.Thread(
            target=rg.run,
            args=(screen, {"flash_seconds": 0.4}, stop),
            daemon=True)
        worker.start()
        try:
            deadline = time.time() + 5
            while not screen.frames and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(screen.frames, "no initial frame")
            plain_bytes = screen.frames[-1].tobytes()

            screen.taps.append({"cell": 5})
            deadline = time.time() + 5
            while len(screen.frames) < 2 and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(len(screen.frames), 2)
            self.assertNotEqual(screen.frames[-1].tobytes(), plain_bytes)

            # Flash expiry redraws the plain grid with no new input.
            deadline = time.time() + 5
            while (screen.frames[-1].tobytes() != plain_bytes
                   and time.time() < deadline):
                time.sleep(0.05)
            self.assertEqual(screen.frames[-1].tobytes(), plain_bytes)
        finally:
            stop.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


class AdvertisedTest(unittest.TestCase):
    def test_renderer_loads_and_validates(self):
        found = displayd.load_renderers(
            os.path.join(os.path.dirname(__file__), os.pardir, "renderers"))
        self.assertIn("retro_grid", found)
        entry = found["retro_grid"]
        self.assertNotIn("broken", entry)
        self.assertIn("boxes", entry["params"])
        self.assertIn("tap", entry["inputs"])
        displayd.validate_params({}, entry["params"])
        displayd.validate_params(
            {"boxes": [{"label": "1"}], "columns": 4, "flash_seconds": 1.0},
            entry["params"])
        displayd.validate_value({"cell": 3}, entry["inputs"]["tap"],
                                "value")


if __name__ == "__main__":
    unittest.main()
