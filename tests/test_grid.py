"""The tile component's grid: one definition of where boxes land.

Before this module the arithmetic lived twice -- `picker.grid_geometry`
and `_options_grid.grid_geometry` carried the same gutter rule, the same
division and the same row-major walk -- so these tests pin the thing that
matters: the picker and the options grid ask the component, and a narrow
region becomes ONE usable application column instead of a strip of 69px
tiles.

Run from the repo root:  python3 -m unittest tests.test_grid -v
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from renderers import picker as pk
from renderers import _options_grid as grid_mod
from ui import grid as ui_grid
from ui import tile as ui_tile

W, H = 1920, 1080
BAND = pk.default_rect(288, H)          # a 15-70-15 side band
FULL = pk.default_rect(W, H)


def cols_of(rects):
    """How many columns a rect list actually used."""
    return len({x for x, _y, _w, _h in rects})


class ColumnsTest(unittest.TestCase):
    """The column count: declared, derived, or a safe default."""

    def test_an_explicit_count_wins(self):
        for cols in (1, 2, 3, 4):
            self.assertEqual(ui_grid.columns(FULL, 6, cols=cols), cols)

    def test_a_count_is_clamped_to_the_cap_and_to_the_boxes(self):
        self.assertEqual(ui_grid.columns(FULL, 6, cap=2, cols=4), 2)
        self.assertEqual(ui_grid.columns(FULL, 1, cols=4), 1)
        self.assertEqual(ui_grid.columns(FULL, 6, cap=0, cols=4), 1)

    def test_a_narrow_band_is_one_application_column(self):
        # The 15-70-15 case: three columns here is a 69px tile, a label
        # no wider than the gutter around it.
        self.assertEqual(ui_grid.columns(BAND, 8, cap=3), 1)
        self.assertEqual(cols_of(pk.grid_geometry(BAND, 8)), 1)

    def test_a_wide_region_takes_the_cap(self):
        self.assertEqual(ui_grid.columns(FULL, 6, cap=3), 3)
        self.assertEqual(ui_grid.columns(FULL, 24, cap=3), 3)

    def test_the_derived_count_keeps_a_box_roughly_square(self):
        # The rule is the column count whose box is closest to square, so
        # a wide region tiles across and a tall one tiles down.
        wide = [160, 40, 1600, 300]
        tall = [160, 40, 400, 1600]
        self.assertEqual(ui_grid.columns(wide, 6, cap=6), 6)
        self.assertEqual(ui_grid.columns(tall, 6, cap=6), 1)

    def test_two_boxes_and_one_box_stay_on_one_row(self):
        self.assertEqual(ui_grid.columns(FULL, 1, cap=3), 1)
        self.assertEqual(ui_grid.columns(FULL, 2, cap=3), 2)

    def test_garbage_has_an_answer(self):
        for rect in (None, "nope", [], [1], [1, 2, 3, 4, 5], (0, 0, -4, -4)):
            for count in (None, "x", 0, -3, 3.7, True, 10 ** 9):
                with self.subTest(rect=rect, count=count):
                    self.assertGreaterEqual(
                        ui_grid.columns(rect, count), 1)
        for cols in (None, "x", 0, -3, 2.9, object()):
            with self.subTest(cols=cols):
                self.assertGreaterEqual(
                    ui_grid.columns(FULL, 6, cap=3, cols=cols), 1)
        self.assertGreaterEqual(ui_grid.columns(FULL, 6, cap=None), 1)


class GridTest(unittest.TestCase):
    """Where each box lands: inside the rect, row-major, never a raise."""

    def test_row_major_inside_the_rect(self):
        rect = FULL
        rects = ui_grid.grid(rect, 5, 2)
        self.assertEqual(len(rects), 5)
        rows = {}
        for i, (x, y, w, h) in enumerate(rects):
            rows.setdefault(y, []).append(x)
            self.assertGreaterEqual(x, rect[0])
            self.assertGreaterEqual(y, rect[1])
            self.assertLessEqual(x + w, rect[0] + rect[2])
            self.assertLessEqual(y + h, rect[1] + rect[3])
        self.assertEqual(rows[rects[0][1]], sorted(rows[rects[0][1]]))
        self.assertEqual(len(rows), 3, "five boxes in two columns is 3 rows")

    def test_the_count_is_bounded(self):
        # A public geometry function handed 10**9 used to allocate a rect
        # per box, which is an out-of-memory rather than a validation
        # error. The component is where that bound now lives.
        self.assertEqual(len(ui_grid.grid(FULL, 10 ** 9, 1)),
                         ui_grid.TILE_CAP)
        self.assertEqual(len(ui_grid.grid(FULL, -5, 1)), 1)

    def test_the_gutter_is_stated_or_derived(self):
        self.assertEqual(ui_grid.grid([0, 0, 200, 200], 2, 1,
                                      gutter=0)[0][0], 0)
        tight = ui_grid.grid([0, 0, 200, 200], 4, 2, gutter=0)
        loose = ui_grid.grid([0, 0, 200, 200], 4, 2)
        self.assertGreater(loose[0][0], tight[0][0])
        self.assertEqual(ui_grid.grid([0, 0, 200, 200], 2, 1,
                                      gutter=-10)[0][0], 0)
        # Derived: the short side over GUTTER_DIV, floored.
        self.assertEqual(ui_grid.grid([0, 0, 200, 200], 2, 1)[0][0],
                         ui_grid.GUTTER_MIN)
        self.assertEqual(ui_grid.grid([0, 0, 2000, 1000], 2, 1)[0][0],
                         1000 // ui_grid.GUTTER_DIV)

    def test_garbage_never_raises(self):
        for rect in (None, "nope", [1], [0, 0, 0, 0]):
            for count in (None, "x", 0, 10 ** 9):
                for cols in (None, "x", 0, -2, 99):
                    with self.subTest(rect=rect, count=count, cols=cols):
                        rects = ui_grid.grid(rect, count, cols)
                        self.assertTrue(rects)
                        for x, y, w, h in rects:
                            self.assertGreaterEqual(w, 1)
                            self.assertGreaterEqual(h, 1)

    def test_a_box_is_never_narrower_than_one_pixel(self):
        rects = ui_grid.grid([0, 0, 9, 9], 48, 6, gutter=0)
        self.assertEqual(len(rects), 48)
        self.assertTrue(all(w >= 1 and h >= 1 for _x, _y, w, h in rects))


class OneDefinitionTest(unittest.TestCase):
    """Both surfaces ask the component; neither restates the arithmetic."""

    def test_the_picker_delegates_and_keeps_only_its_cap(self):
        self.assertEqual(pk.grid_geometry(FULL, 6, cols=2),
                         ui_grid.grid(FULL, 6, 2))
        self.assertEqual(cols_of(pk.grid_geometry(FULL, 6, cols=9)),
                         pk.DEFAULT_COLS)

    def test_the_options_grid_delegates_and_keeps_its_policy(self):
        rect = grid_mod.grid_rect(W, H)
        self.assertEqual(grid_mod.grid_geometry(rect, 4),
                         ui_grid.grid(rect, 4, grid_mod.cols_for(4)))
        self.assertEqual(grid_mod.cols_for(2), 1)
        self.assertEqual(grid_mod.cols_for(3), 2)
        self.assertEqual(grid_mod.MAX_CELLS, ui_grid.TILE_CAP)

    def test_neither_surface_restates_the_grid_arithmetic(self):
        root = os.path.join(os.path.dirname(__file__), os.pardir,
                            "renderers")
        for name in ("picker.py", "_options_grid.py"):
            with open(os.path.join(root, name)) as handle:
                src = handle.read()
            with self.subTest(module=name):
                self.assertIsNone(
                    re.search(r"rows\s*=\s*\(count\s*\+", src),
                    "%s recomputes the row count" % name)
                self.assertNotIn("GUTTER_DIV", src)
                self.assertNotIn("// 45", src)
                self.assertNotIn("rw - (cols + 1)", src)

    def test_explicit_cols_reaches_the_touch_regions(self):
        wide = pk.picker_regions(W, H, ["clock", "chat", "row"], cols=1)
        self.assertEqual(len(wide), 3)
        self.assertEqual(cols_of([e["rect"] for e in wide]), 1)
        band = pk.picker_regions(288, H, ["clock", "chat", "row", "beeps"])
        self.assertEqual(cols_of([e["rect"] for e in band]), 1)

    def test_the_band_tile_leaves_room_for_its_label(self):
        # The point of the single column: a label that fits at a readable
        # size, rather than the shrunk-to-the-floor text of a 69px tile.
        band = pk.grid_geometry(BAND, 8)[0]
        three = ui_grid.grid(BAND, 8, 3)[0]
        roomy = ui_tile.fit_size("touch_confidence", band[2])
        cramped = ui_tile.fit_size("touch_confidence", three[2])
        self.assertGreater(roomy, cramped)
        self.assertGreater(roomy, ui_tile.LABEL_MIN)


if __name__ == "__main__":
    unittest.main()
