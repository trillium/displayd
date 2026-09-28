"""Tests for the V1 proportional-parade column widths. No framebuffer needed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from renderers import beads
from renderers import beads_layout as layout


AVAIL = 1800.0  # 1920 panel minus PAD on both sides, as the renderer uses it
LIVE = [29, 3628, 145, 4524]  # rolling, linedup, stalled, past (2026-09-28)


class TestColumnWidths(unittest.TestCase):
    def test_widths_tile_avail_exactly(self):
        widths = layout.column_widths(LIVE, AVAIL)
        self.assertEqual(len(widths), 4)
        self.assertAlmostEqual(sum(widths), AVAIL, places=6)

    def test_queue_dwarfs_rolling_but_rolling_survives(self):
        widths = layout.column_widths(LIVE, AVAIL)
        rolling, linedup, stalled, past = widths
        # The glance tells the true story: queue and done own the panel.
        self.assertGreater(linedup, rolling)
        self.assertGreater(past, linedup)
        # ...while the floors keep the small buckets legible.
        self.assertGreaterEqual(rolling, layout.FLOORS["rolling"])
        self.assertGreaterEqual(stalled, layout.FLOORS["stalled"])

    def test_sqrt_flatters_small_buckets(self):
        # Strict linear share would give Rolling ~6px of 1800; sqrt must
        # give it visibly more (floor or compresssed share, never linear).
        widths = layout.column_widths(LIVE, AVAIL)
        linear = AVAIL * 29 / sum(LIVE)
        self.assertGreater(widths[0], linear)

    def test_all_empty_gives_equal_columns(self):
        widths = layout.column_widths([0, 0, 0, 0], AVAIL)
        self.assertEqual(widths, [AVAIL / 4] * 4)

    def test_one_bucket_dominant(self):
        widths = layout.column_widths([0, 0, 0, 100], AVAIL)
        rolling, linedup, stalled, past = widths
        self.assertEqual(rolling, layout.FLOORS["rolling"])
        self.assertEqual(stalled, layout.FLOORS["stalled"])
        self.assertEqual(linedup, 0.0)
        self.assertAlmostEqual(sum(widths), AVAIL, places=6)
        self.assertGreater(past, AVAIL / 2)

    def test_bucket_exactly_at_floor_keeps_floor(self):
        # A bucket whose raw share lands exactly on its floor is neither
        # above (no give) nor below (no take): it keeps exactly the floor.
        floors = [100.0, 0.0, 0.0, 0.0]
        # sqrt weights w,3w... solve raw[0] == 100 with avail 1000:
        # raw[0] = 1000*w0/(w0+w1) = 100 -> w0/(w0+w1) = 0.1.
        # counts [1, 81]: sqrt 1 and 9 -> 1000*1/10 = 100. Exact.
        widths = layout.column_widths([1, 81], 1000.0, floors)
        self.assertEqual(widths[0], 100.0)
        self.assertAlmostEqual(sum(widths), 1000.0, places=6)

    def test_floors_never_violated_live_shape(self):
        for counts in ([29, 3628, 145, 4524], [1, 1, 1, 1],
                       [0, 5000, 0, 0], [3, 0, 2, 0]):
            widths = layout.column_widths(counts, AVAIL)
            fl = [layout.FLOORS[k] for k in
                  ("rolling", "linedup", "stalled", "past")]
            for got, floor in zip(widths, fl):
                self.assertGreaterEqual(got, floor - 1e-6)
            self.assertAlmostEqual(sum(widths), AVAIL, places=6)

    def test_avail_smaller_than_floors_scales_down(self):
        widths = layout.column_widths(LIVE, 100.0)
        self.assertAlmostEqual(sum(widths), 100.0, places=6)
        self.assertTrue(all(w >= 0 for w in widths))

    def test_zero_avail_gives_zeros(self):
        self.assertEqual(layout.column_widths(LIVE, 0), [0.0] * 4)


class TestSkewedDraw(unittest.TestCase):
    def test_narrow_columns_draw_without_error(self):
        # Live-shaped snapshot: Rolling/Stalled columns hit their floors
        # and take the narrow-caption branch. Must render, never raise.
        from PIL import Image
        import displayd
        import time as _t

        class FakeScreen:
            W, H = 1920, 1080

            def __init__(self):
                self.frames = []

            def new_image(self, background=(0, 0, 0)):
                return Image.new("RGB", (self.W, self.H), background)

            def present(self, img):
                self.frames.append(img.copy())

            @classmethod
            def color(cls, value, default=(255, 255, 255)):
                return displayd.Screen.color(value, default)

            @staticmethod
            def font_path(family="DejaVuSans-Bold"):
                return displayd.Screen.font_path(family)

        def raw(iid, status="open", deps=()):
            return {"id": iid, "title": "title " + iid,
                    "status": status, "priority": 2, "labels": [],
                    "dependencies": [
                        {"depends_on_id": t, "type": k} for k, t in deps]}

        pairs = ([("task", raw("r%d" % i, "in_progress"))
                  for i in range(35)]
                 + [("task", raw("l%d" % i)) for i in range(3221)]
                 + [("task", raw("s%d" % i, deps=[("blocks", "x%d" % i)]))
                    for i in range(151)]
                 + [("task", raw("x%d" % i)) for i in range(151)]
                 + [("task", raw("p%d" % i, "closed"))
                    for i in range(3219)])
        snap = beads._classify(pairs)
        self.assertEqual(len(snap["rolling"]), 35)
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=snap, updated=_t.time(),
                               health="warm", error=None, source="test")
        try:
            screen = FakeScreen()
            beads._draw(screen, "BEADS", (8, 8, 12))
            self.assertTrue(screen.frames)
            self.assertTrue(any(screen.frames[-1].tobytes()))
        finally:
            with beads._POLL["lock"]:
                beads._POLL.update(snapshot=None, updated=0.0,
                                   health="cold", error=None, source=None)


if __name__ == "__main__":
    unittest.main()
