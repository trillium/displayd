"""The progress component: one definition of the playlist's bar.

Before this module the bar's geometry and compositing lived twice --
``playlist_bar`` (the module the docstring named as the owner) and a
byte-identical copy in ``playlist`` that shadowed its own import of the
former -- so these tests pin the thing that matters: the bar is drawn in
the component layer from palette roles, the geometry is the one rule, and
``playlist`` composes the component without drawing.

Run from the repo root:  python3 -m unittest tests.test_progress -v
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from PIL import Image
from renderers.ui import progress as ui_progress
import playlist as playlist_module
import theme

REPO = os.path.dirname(os.path.abspath(playlist_module.__file__))
OLD_TRACK = (38, 38, 46)          # playlist_color.TRACK_COLOR before this
OLD_DARK_BORDER = (10, 10, 12)    # playlist._contrast's bright-fill branch
OLD_LIGHT_BORDER = (235, 235, 240)


def frame(width=160, height=90, color=None):
    return Image.new("RGB", (width, height),
                     color or theme.rgb("page"))


def strip_row(img, y):
    return [img.getpixel((x, y)) for x in range(img.size[0])]


class GeometryTest(unittest.TestCase):
    """The one rule: which strip is the track, and where the fill lands."""

    def test_every_placement_owns_its_own_edge(self):
        W, H, t = 160, 90, 10
        self.assertEqual(ui_progress.boxes(W, H, "bottom", t, 0.5)[0],
                         (0, H - t, W, H))
        self.assertEqual(ui_progress.boxes(W, H, "top", t, 0.5)[0],
                         (0, 0, W, t))
        self.assertEqual(ui_progress.boxes(W, H, "left", t, 0.5)[0],
                         (0, 0, t, H))
        self.assertEqual(ui_progress.boxes(W, H, "right", t, 0.5)[0],
                         (W - t, 0, W, H))

    def test_the_fill_grows_left_to_right_and_rises_on_a_side_edge(self):
        W, H, t = 160, 90, 10
        self.assertEqual(ui_progress.boxes(W, H, "bottom", t, 0.5)[1],
                         (0, H - t, W // 2, H))
        # A vertical bar fills from the bottom up, like a meter rising.
        self.assertEqual(ui_progress.boxes(W, H, "left", t, 0.5)[1],
                         (0, H - H // 2, t, H))
        self.assertEqual(ui_progress.boxes(W, H, "right", t, 1.0)[1],
                         (W - t, 0, W, H))

    def test_an_empty_fill_is_a_track_not_a_missing_bar(self):
        track, fill = ui_progress.boxes(160, 90, "bottom", 10, 0.0)
        self.assertIsNotNone(track)
        self.assertIsNone(fill)

    def test_a_thin_or_garbage_thickness_still_draws(self):
        for thickness in (0, -5, None, "nonsense"):
            track, _ = ui_progress.boxes(160, 90, "bottom", thickness, 0.5)
            self.assertGreaterEqual(track[3] - track[1],
                                    ui_progress.EDGE)

    def test_an_unknown_placement_is_the_one_loud_path(self):
        with self.assertRaises(ValueError):
            ui_progress.boxes(160, 90, "diagonal", 10, 0.5)


class DirectionTest(unittest.TestCase):
    """``drain`` counts a dwell down; anything unknown reads as fill."""

    def test_drain_reverses_and_fill_does_not(self):
        self.assertEqual(ui_progress.shown(0.25, "fill"), 0.25)
        self.assertEqual(ui_progress.shown(0.25, "drain"), 0.75)

    def test_the_share_is_clamped_and_total(self):
        self.assertEqual(ui_progress.shown(-1, "fill"), 0.0)
        self.assertEqual(ui_progress.shown(4, "fill"), 1.0)
        self.assertEqual(ui_progress.shown(None, "fill"), 0.0)
        self.assertEqual(ui_progress.shown("junk", "fill"), 0.0)


class ContrastTest(unittest.TestCase):
    """The border is a palette role, not a literal pair."""

    def test_a_bright_fill_takes_the_dark_role(self):
        self.assertEqual(ui_progress.contrast((255, 255, 255)),
                         theme.rgb("on-accent"))
        self.assertEqual(ui_progress.contrast((240, 240, 240)),
                         theme.rgb("on-accent"))

    def test_a_dark_fill_takes_the_bright_role(self):
        self.assertEqual(ui_progress.contrast((20, 20, 30)),
                         theme.rgb("ink-strong"))

    def test_the_old_border_literals_are_gone(self):
        self.assertNotEqual(ui_progress.contrast((255, 255, 255)),
                            OLD_DARK_BORDER)
        self.assertNotEqual(ui_progress.contrast((10, 10, 10)),
                            OLD_LIGHT_BORDER)

    def test_a_colour_it_cannot_measure_still_answers(self):
        self.assertEqual(ui_progress.contrast(None), theme.rgb("on-accent"))


class DrawnBarTest(unittest.TestCase):
    """The frame it produces, pixel by pixel."""

    def test_the_track_is_the_palette_token(self):
        # The token was moved onto the palette from playlist_color's own
        # literal, so the strip's pixels are unchanged and the old value
        # is now the token rather than a second copy of it.
        self.assertEqual(theme.rgb("track"), OLD_TRACK)
        img = progress_bottom(0.5, (255, 64, 64))
        row = strip_row(img, 85)
        self.assertEqual(row.count(theme.rgb("track")), 79)
        self.assertEqual(row.count((255, 64, 64)), 79)

    def test_the_fill_wears_the_contrast_border(self):
        colour = (255, 64, 64)
        img = progress_bottom(0.5, colour)
        border = ui_progress.contrast(colour)
        self.assertEqual(border, theme.rgb("ink-strong"))
        self.assertEqual(img.getpixel((0, 85)), border)
        self.assertEqual(img.getpixel((80, 85)), border)
        self.assertEqual(img.getpixel((81, 85)), theme.rgb("track"))

    def test_a_drain_bar_is_longer_at_the_same_fraction(self):
        filled = progress_bottom(0.25, (255, 64, 64))
        drained = progress_bottom(0.25, (255, 64, 64), direction="drain")
        red = (255, 64, 64)
        self.assertGreater(strip_row(drained, 85).count(red),
                           strip_row(filled, 85).count(red))

    def test_a_full_bar_leaves_no_track(self):
        colour = (255, 64, 64)
        img = progress_bottom(1.0, colour)
        row = strip_row(img, 85)
        self.assertEqual(row.count(theme.rgb("track")), 0)
        # The geometry's boxes are inclusive of their last coordinate, so
        # a full bar's right border lands one pixel past the panel edge
        # and only its left border is visible.
        self.assertEqual(row.count(ui_progress.contrast(colour)), 1)
        self.assertEqual(row.count(colour), len(row) - 1)


def progress_bottom(fraction, color, direction="fill"):
    img = frame()
    ui_progress.draw(img, "bottom", 10, fraction, direction, color)
    return img


class NeverBlanksTest(unittest.TestCase):
    """A bar that cannot be drawn is a missing bar, never a broken frame."""

    def test_an_unknown_placement_leaves_the_frame_untouched(self):
        img = frame()
        before = img.tobytes()
        self.assertIs(ui_progress.draw(img, "diagonal", 10, 0.5, "fill",
                                       (255, 0, 0)), img)
        self.assertEqual(img.tobytes(), before)

    def test_a_colour_that_is_not_one_draws_nothing(self):
        for color in (object(), "#ffffff", (1, 2), (300, 0, 0), None):
            img = frame()
            before = img.tobytes()
            ui_progress.draw(img, "bottom", 10, 0.5, "fill", color)
            self.assertEqual(img.tobytes(), before, "drew with %r" % (color,))

    def test_a_one_pixel_frame_does_not_raise(self):
        img = frame(1, 1)
        ui_progress.draw(img, "top", 10, 0.5, "fill", (255, 0, 0))


class OneDefinitionTest(unittest.TestCase):
    """The duplication is gone, in both directions."""

    def read(self, name):
        with open(os.path.join(REPO, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_shadowed_duplicate_is_gone(self):
        # playlist.py used to redefine bar_boxes/draw_bar/_contrast after
        # importing them, so its own copy won at every call site.
        source = self.read("playlist.py")
        self.assertNotIn("ImageDraw", source)
        self.assertNotIn("def bar_boxes", source)
        self.assertNotIn("def draw_bar", source)
        self.assertNotIn("def _contrast", source)

    def test_the_old_owner_module_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(REPO,
                                                     "playlist_bar.py")))

    def test_the_playlist_asks_the_component(self):
        self.assertIs(playlist_module.draw_bar, ui_progress.draw)
        self.assertIs(playlist_module.bar_boxes, ui_progress.boxes)
        self.assertEqual(playlist_module.PLACEMENTS,
                         ui_progress.PLACEMENTS)
        self.assertEqual(playlist_module.DIRECTIONS,
                         ui_progress.DIRECTIONS)

    def test_the_gate_no_longer_exempts_either_module(self):
        gate = self.read(os.path.join("tools", "check-components.py"))
        exemptions = re.search(r"EXEMPTIONS = \((.*?)\)\n\n", gate,
                               re.S).group(1)
        self.assertNotIn("playlist.py", exemptions)
        self.assertNotIn("playlist_bar.py", exemptions)
        self.assertIn('"renderers/_html_native.py"', exemptions)

    def test_the_track_colour_has_one_owner(self):
        self.assertEqual(theme.rgb("track"), OLD_TRACK)
        self.assertNotIn("TRACK_COLOR", self.read("playlist_color.py"))
        self.assertNotIn("38, 38, 46", self.read("playlist_color.py"))


if __name__ == "__main__":
    unittest.main()
