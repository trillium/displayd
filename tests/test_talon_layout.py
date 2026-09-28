"""Side-button layout tests (renderers/talon_layout.py).

Grouping by display, even-split degrade, button/column geometry, and
hit-testing -- all pure, no panel needed.

Run from the repo root:  python3 -m unittest tests.test_talon_layout -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import talon_layout as layout

W, H = 1920, 1080
TWO = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117}, "main": True},
       {"bounds": {"x": -1692, "y": -135, "w": 1692, "h": 945},
        "main": False}]


def windows(*placed):
    return {name: {"x": x, "y": y, "d": d}
            for name, x, y, d in placed}


class GroupTest(unittest.TestCase):
    def test_two_displays_group_by_leftmost(self):
        apps = ["A", "B", "C"]
        grouped = layout.group(
            apps, windows(("A", -1000, 0, 1), ("B", 500, 500, 0),
                          ("C", 600, 600, 0)), TWO)
        self.assertEqual(grouped["mode"], "sides")
        self.assertEqual(grouped["left_display"], 1)
        self.assertEqual(grouped["left"], [0])
        self.assertEqual(grouped["right"], [1, 2])
        self.assertEqual(grouped["overflow"], 0)

    def test_unknown_joins_main_side(self):
        # Windowless "Ghost" has no entry: main is display 0 (right
        # here, since display 1 is leftmost), so it sits right.
        grouped = layout.group(["A", "Ghost"],
                               windows(("A", -1000, 0, 1)), TWO)
        self.assertEqual(grouped["left"], [0])
        self.assertEqual(grouped["right"], [1])

    def test_multi_display_app_uses_frontmost_only(self):
        # One entry per app: frontmost window decides, no double-seat.
        grouped = layout.group(["A"], windows(("A", -1000, 0, 1)), TWO)
        self.assertEqual(grouped["left"], [0])
        self.assertEqual(grouped["right"], [])

    def test_single_display_splits_evenly(self):
        one = [TWO[0]]
        grouped = layout.group(["A", "B", "C"], windows(
            ("A", 10, 10, 0), ("B", 20, 20, 0), ("C", 30, 30, 0)), one)
        self.assertEqual(grouped["mode"], "split")
        self.assertEqual(grouped["left"], [0, 1])
        self.assertEqual(grouped["right"], [2])

    def test_no_display_info_splits_evenly(self):
        grouped = layout.group(["A", "B", "C", "D"])
        self.assertEqual(grouped["mode"], "split")
        self.assertEqual(grouped["left"], [0, 1])
        self.assertEqual(grouped["right"], [2, 3])

    def test_no_empty_side_single_app(self):
        grouped = layout.group(["Solo"])
        self.assertEqual(grouped["left"], [0])
        self.assertEqual(grouped["right"], [])

    def test_overflow_caps_per_side(self):
        apps = ["A%d" % i for i in range(25)]
        grouped = layout.group(apps)
        self.assertEqual(len(grouped["left"]), layout.MAX_PER_SIDE)
        self.assertEqual(len(grouped["right"]), layout.MAX_PER_SIDE)
        self.assertEqual(grouped["overflow"],
                         25 - 2 * layout.MAX_PER_SIDE)

    def test_garbage_never_raises(self):
        for apps, win, disp in [(None, None, None), ("nope", {}, []),
                                ([None, 42], {"x": 1}, [{"bounds": {}}]),
                                ([], None, TWO)]:
            grouped = layout.group(apps, win, disp)
            self.assertIn("left", grouped)
            self.assertIn("right", grouped)


class GeometryTest(unittest.TestCase):
    def test_columns_mirror_and_touch_rects_match(self):
        left = layout.button_rect("left", 0, W)
        right = layout.button_rect("right", 0, W)
        self.assertEqual(left[0], layout.PAD)
        self.assertEqual(right[0], W - layout.PAD - layout.COL_W)
        self.assertEqual(left[2], right[2], layout.COL_W)
        # Touch strips cover the button columns exactly.
        self.assertEqual(layout.column_rect("left", W, H),
                         [48, 250, 560, 782])
        self.assertEqual(layout.column_rect("right", W, H),
                         [1312, 250, 560, 782])

    def test_hit_round_trips_every_slot(self):
        apps = ["A%d" % i for i in range(2 * layout.MAX_PER_SIDE)]
        grouped = layout.group(apps)
        for side in ("left", "right"):
            for slot, index in enumerate(grouped[side]):
                x, y, w, h = layout.button_rect(side, slot, W)
                self.assertEqual(
                    layout.hit(x + 3, y + 3, W, H, grouped), index)

    def test_hit_misses(self):
        grouped = layout.group(["A", "B"])
        self.assertIsNone(layout.hit(960, 300, W, H, grouped))  # gap
        self.assertIsNone(layout.hit(100, 100, W, H, grouped))  # header
        self.assertIsNone(layout.hit(100, 300, W, H, None))
        self.assertIsNone(layout.hit(100, 300, W, H, "junk"))
        self.assertIsNone(layout.hit("x", 300, W, H, grouped))


if __name__ == "__main__":
    unittest.main()
