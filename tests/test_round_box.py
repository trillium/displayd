"""Rounded corners on the Pillow-drawn surfaces: card, strip and tile.

Pixel assertions, not geometry ones: a call into ``round_box`` would pass
whether or not ``ui.panel`` / ``ui.tile`` ever used it, and the bug here is
a shape that still renders square. So each test draws the real component and
counts what lies in a corner patch -- before the change every patch is solid
fill; after it a corner covers fewer fill pixels, and the pixels between are
blends of fill and backdrop (anti-aliased), not just the two flat colours.

Run from the repo root:  python3 -m unittest tests.test_round_box -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "renderers"))

from PIL import Image  # noqa: E402

import _html_error  # noqa: E402
import theme  # noqa: E402
from ui import round_box  # noqa: E402
from ui import tile as ui_tile  # noqa: E402
from tests.test_panel import FakeScreen, count  # noqa: E402

PATCH = round_box.RADIUS


def patches(img, x, y, w, h):
    """The four RADIUS-square corner patches of the box (x, y, w, h)."""
    p = PATCH
    return [img.crop((cx, cy, cx + p, cy + p)) for cx, cy in (
        (x, y), (x + w - p, y), (x, y + h - p), (x + w - p, y + h - p))]


def blends(patch, *flat):
    """Pixels that are neither flat colour: the anti-aliased edge."""
    return sum(n for n, c in patch.getcolors(maxcolors=1 << 16)
               if c not in flat)


class TestRoundedCorners(unittest.TestCase):
    def setUp(self):
        self.screen = FakeScreen()
        self.page = theme.rgb("page")

    def assert_rounded(self, corners, fill):
        for i, patch in enumerate(corners):
            with self.subTest(corner=i):
                self.assertLess(count(patch, fill), PATCH * PATCH - 12)
                self.assertGreater(blends(patch, fill, self.page), 0,
                                   "corner is aliased, not anti-aliased")

    def test_the_failure_card_has_four_rounded_corners(self):
        frame = _html_error.error_frame(self.screen, "boom", "fix it")
        w, h = self.screen.W, self.screen.H
        # Every corner patch used to be the card's own fill or bar.
        corners = patches(frame, 0, 0, w, h)
        for i, patch in enumerate(corners):
            with self.subTest(corner=i):
                self.assertGreater(count(patch, self.page), 12)
                self.assertGreater(blends(patch, self.page,
                                          theme.rgb("alert-page"),
                                          theme.rgb("alert")), 0)
        # ... while the fill, the bar and the edge midpoints are unchanged.
        self.assertEqual(frame.getpixel((w // 2, 1)), theme.rgb("alert"))
        self.assertEqual(frame.getpixel((w // 2, h - 1)),
                         theme.rgb("alert-page"))

    def test_the_strip_rounds_its_own_rect_and_nothing_around_it(self):
        rect = (80, 770, 1840, 230)
        frame = _html_error.error_strip(self.screen, rect, "boom", "fix it")
        for i, patch in enumerate(patches(frame, *rect)):
            with self.subTest(corner=i):
                self.assertGreater(count(patch, self.page), 12)
        outside = frame.crop((0, 0, self.screen.W, rect[1]))
        self.assertEqual(count(outside, self.page), 0)

    def test_a_filled_tile_has_four_rounded_corners(self):
        fill = (200, 60, 60)
        img = Image.new("RGB", (480, 360), self.page)
        ui_tile.draw(img, (20, 20, 420, 320), "tile", fill=fill)
        self.assert_rounded(patches(img, 20, 20, 420, 320), fill)

    def test_an_outlined_tile_rounds_its_outline(self):
        ink = (250, 250, 250)
        img = Image.new("RGB", (480, 360), self.page)
        ui_tile.draw(img, (20, 20, 420, 320), "", outline=ink, width=4)
        for i, patch in enumerate(patches(img, 20, 20, 420, 320)):
            with self.subTest(corner=i):
                self.assertGreater(count(patch, self.page), 12)
                self.assertGreater(blends(patch, ink, self.page), 0)
        # The edge itself is still drawn, one outline wide, at its midpoint.
        self.assertEqual(img.getpixel((230, 20)), ink)
        self.assertEqual(img.getpixel((230, 22)), ink)
        self.assertEqual(img.getpixel((230, 24)), self.page)

    def test_a_tiny_box_is_a_box_not_a_crash(self):
        img = Image.new("RGB", (40, 40), self.page)
        for rect in ((5, 5, 1, 1), (5, 5, 3, 30), (0, 0, 0, 0)):
            with self.subTest(rect=rect):
                self.assertIs(img, ui_tile.draw(img, rect, "x",
                                                fill=(1, 2, 3)))


if __name__ == "__main__":
    unittest.main()
