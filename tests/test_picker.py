"""Tests for the picker renderer. No framebuffer or touch hardware needed.

Run from the repo root:  python3 -m pytest tests/test_picker.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import touch
from PIL import Image

from renderers import picker as pk

W, H = 1920, 1080
VIEWS = ["clock", "chat", "row", "stream", "activity", "options"]


class FakeScreen:
    W, H = W, H

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return default

    def font_path(self, family="DejaVuSans-Bold"):
        return None


class CoerceViewsTest(unittest.TestCase):
    def test_default_six(self):
        self.assertEqual(pk.coerce_views({}), list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.coerce_views(None), list(pk.DEFAULT_VIEWS))

    def test_garbage_falls_back(self):
        self.assertEqual(pk.coerce_views({"views": "clock"}),
                         list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.coerce_views({"views": []}),
                         list(pk.DEFAULT_VIEWS))

    def test_skips_bad_entries(self):
        self.assertEqual(pk.coerce_views({"views": ["clock", "", "a/b",
                                                    None, 5, " chat "]}),
                         ["clock", "chat"])

    def test_capped_at_max_views(self):
        many = ["v%d" % i for i in range(30)]
        self.assertEqual(pk.coerce_views({"views": many}),
                         many[:pk.MAX_VIEWS])
        self.assertEqual(pk.MAX_VIEWS, 24)


class LiveViewsTest(unittest.TestCase):
    def test_filters_param_gated_and_broken(self):
        registry = {
            "clock": {"module": object(), "params": {}},
            "macbook": {"module": object(),
                          "params": {"title": {"type": "string"}}},
            "reload": {"module": object(),
                         "params": {"sha": {"type": "string",
                                              "required": True}}},
            "notice": {"module": object(),
                         "params": {"title": {"type": "string",
                                                "required": True}}},
            "broken": {"broken": "boom"},
            "a/b": {"module": object(), "params": {}},
        }
        self.assertEqual(pk.live_views(registry), ["clock", "macbook"])

    def test_garbage_falls_back(self):
        self.assertEqual(pk.live_views(None), list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.live_views({}), list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.live_views("nope"), list(pk.DEFAULT_VIEWS))


class RectTest(unittest.TestCase):
    def test_default_rect_leaves_strips_and_bar(self):
        self.assertEqual(pk.default_rect(1920, 1080), [160, 40, 1600, 860])

    def test_explicit_rect_kept(self):
        self.assertEqual(pk.coerce_rect({"rect": [10, 20, 300, 400]},
                                        W, H), [10, 20, 300, 400])

    def test_garbage_rect_falls_back(self):
        self.assertEqual(pk.coerce_rect({"rect": "nope"}, W, H),
                         pk.default_rect(W, H))
        self.assertEqual(pk.coerce_rect({"rect": [0, 0, -5, 0]}, W, H),
                         pk.default_rect(W, H))


class GeometryTest(unittest.TestCase):
    def test_six_tiles_inside_rect_no_overlap(self):
        rect = pk.default_rect(W, H)
        geo = pk.grid_geometry(rect, 6)
        self.assertEqual(len(geo), 6)
        seen = set()
        for x, y, cw, ch in geo:
            self.assertGreaterEqual(x, rect[0])
            self.assertGreaterEqual(y, rect[1])
            self.assertLessEqual(x + cw, rect[0] + rect[2])
            self.assertLessEqual(y + ch, rect[1] + rect[3])
            for px in (x, x + cw - 1):
                for py in (y, y + ch - 1):
                    self.assertNotIn((px, py), seen)
                    seen.add((px, py))

    def test_single_tile_is_one_column(self):
        geo = pk.grid_geometry(pk.default_rect(W, H), 1)
        self.assertEqual(len(geo), 1)


class RegionsTest(unittest.TestCase):
    def test_tiles_fire_select_view(self):
        regions = pk.picker_regions(W, H, VIEWS)
        self.assertEqual([r["id"] for r in regions],
                         ["view-%s" % v for v in VIEWS])
        for region, view in zip(regions, VIEWS):
            method, path, body = touch.action_request(region["action"])
            self.assertEqual((method, path), ("POST", "/show"))
            self.assertEqual(body, {"renderer": view, "params": {}})

    def test_generator_matches_cli(self):
        # The touch.json procedure shells out to the module: the printed
        # regions must equal the library call with the same arguments.
        proc = subprocess.run(
            [sys.executable, "renderers/picker.py",
             "--width", str(W), "--height", str(H),
             "--views", ",".join(VIEWS)],
            capture_output=True, text=True, cwd=os.path.join(
                os.path.dirname(__file__), os.pardir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         pk.picker_regions(W, H, VIEWS))

    def test_shipped_example_matches_module(self):
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "touch-picker.json.example")
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["width"], 1920)
        self.assertEqual(doc["height"], 1080)
        self.assertEqual(doc["tap_options"]["renderer"], "picker")
        tiles = [r for r in doc["regions"] if r["id"].startswith("view-")]
        views = [r["action"]["view"] for r in tiles]
        # The example is generated from the live set, not hand-written:
        # it must still expose the views this fix is for.
        self.assertIn("macbook", views)
        self.assertEqual([r["id"] for r in tiles],
                         ["view-%s" % v for v in views])
        self.assertEqual(tiles, pk.picker_regions(
            1920, 1080, views, rect=pk.default_rect(1920, 1080)))
        for region, view in zip(tiles, views):
            method, path_, body = touch.action_request(region["action"])
            self.assertEqual((method, path_), ("POST", "/show"))
            self.assertEqual(body, {"renderer": view, "params": {}})
        # Selection tiles come first: earlier entries win every overlap.
        first_non_tile = next(i for i, r in enumerate(doc["regions"])
                              if not r["id"].startswith("view-"))
        self.assertTrue(all(r["id"].startswith("view-")
                            for r in doc["regions"][:first_non_tile]))
        # The example itself validates (minus its comment key).
        cfg = dict(doc)
        cfg.pop("_comment", None)
        for region in cfg["regions"]:
            region.pop("_comment", None)
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(cfg, fh)
            tmp = fh.name
        try:
            loaded = touch.load_config(tmp)
        finally:
            os.unlink(tmp)
        self.assertTrue(loaded["tap_options"]["enabled"])


class DrawTest(unittest.TestCase):
    def test_run_presents_one_full_frame(self):
        screen = FakeScreen()
        stop = threading.Event()
        pk.run(screen, {"views": VIEWS}, stop)
        self.assertEqual(len(screen.frames), 1)
        self.assertEqual(screen.frames[0].size, (W, H))

    def test_run_survives_garbage_params(self):
        screen = FakeScreen()
        pk.run(screen, {"views": "nope", "rect": [0, 0, -1, -1],
                        "background": "nope"}, threading.Event())
        self.assertEqual(len(screen.frames), 1)


if __name__ == "__main__":
    unittest.main()
