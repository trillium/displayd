"""The panel component -- and the three card views it migrated.

A titled region with a body was hand-drawn three times: ``notice`` (a
severity bar, a headline, a body, a corner tag), ``text`` (one auto-fitted
message) and ``sleep`` (a centred hint). Between them they carried three
font loaders, two different "make the words fit" searches and four colour
literals -- ``notice``'s severity map alone held three RGB tuples.

This pins the one definition:

- the fit rule scales a whole block to the region and never truncates it,
  and the size it returns is the LARGEST one that fits;
- the card's bar, headline, body and tag come from the palette, and the
  palette's alert red is exactly what the old ``(255, 70, 70)`` literal is
  no longer;
- the three migrated views hold no font loader, no fitting search and no
  drawing primitive of their own -- only the per-view playlist accent,
  which ``theme.ACCENT_SLOTS`` also carries and a later increment folds in.

Run from the repo root:  python3 -m unittest tests.test_panel -v
"""

import os
import re
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from PIL import Image

import displayd
import theme
from ui import panel
from ui import text as ui_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
MIGRATED = ("renderers/notice.py", "renderers/text.py",
            "renderers/sleep.py")
CARDS = {"renderers/notice.py": "notice", "renderers/text.py": "text",
         "renderers/sleep.py": "sleep"}


class FakeScreen:
    """A screen with no fonts at all, like a host without a font package:
    every component has to survive that."""

    def __init__(self, W=1920, H=1080):
        self.W, self.H = W, H
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    color = staticmethod(displayd.Screen.color)
    font_path = staticmethod(displayd.Screen.font_path)


def page(screen=None):
    return (screen or FakeScreen()).new_image(theme.rgb("page"))


def count(img, colour):
    return sum(n for n, c in img.getcolors(maxcolors=1 << 24) if c == colour)


def render(module_name, params, timeout=15.0):
    """Run one card view the way the daemon does, and return its screen."""
    import importlib
    module = importlib.import_module(module_name)
    screen = FakeScreen()
    stop = threading.Event()
    thread = threading.Thread(target=module.run,
                              args=(screen, params, stop), daemon=True)
    thread.start()
    import time
    deadline = time.time() + timeout
    while not screen.frames and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=5.0)
    return screen


class FitTest(unittest.TestCase):
    """One rule: the whole block fits, the face gives way, nothing is cut."""

    def setUp(self):
        self.screen = FakeScreen()

    def test_a_block_that_fits_keeps_the_declared_size(self):
        self.assertEqual(
            panel.fit_size(self.screen, "hello", 110, self.screen.W,
                           self.screen.H), 110)

    def test_a_long_block_shrinks_to_fit_and_is_never_cut(self):
        text = "M" * 60
        size = panel.fit_size(self.screen, text, 400, self.screen.W,
                              self.screen.H)
        self.assertLess(size, 400)
        self.assertGreater(size, panel.FLOOR)  # the face gives way, not the words
        self.assertTrue(panel.fits(self.screen, text, size, self.screen.W,
                                   self.screen.H))

    def test_the_size_it_returns_is_the_largest_that_fits(self):
        text = "W" * 30
        size = panel.fit_size(self.screen, text, 200, self.screen.W,
                              self.screen.H)
        self.assertLess(size, 200)
        self.assertFalse(panel.fits(self.screen, text, size + 1,
                                    self.screen.W, self.screen.H))

    def test_the_margin_keeps_the_type_off_the_region_s_edges(self):
        # A block exactly as wide as the region is already too wide for a
        # card: MARGIN belongs to the component, not to the caller.
        text = "H" * 10
        natural = ui_text.width(self.screen, text, 100)
        self.assertGreater(natural, 0)
        self.assertLess(panel.fit_size(self.screen, text, 100, natural, 0), 100)
        room = natural / panel.MARGIN + 50
        self.assertEqual(panel.fit_size(self.screen, text, 100, room, 0), 100)

    def test_no_room_keeps_the_declared_size_and_garbage_gives_the_floor(self):
        self.assertEqual(panel.fit_size(self.screen, "hello", 110), 110)
        self.assertEqual(panel.fit_size(self.screen, "hello", "junk"), panel.FLOOR)
        self.assertEqual(panel.fit_size(None, "hello", 110, 1920, 1080), 110)

    def test_fits_is_total(self):
        for text, size in ((None, 110), ("", 110), ("hi", None),
                           (object(), 40)):
            with self.subTest(text=text):
                self.assertIsInstance(
                    panel.fits(self.screen, text, size, 1920, 1080), bool)


class CardTest(unittest.TestCase):
    """The component: bar, headline, body, tag -- all optional but the
    headline, all from the palette unless the caller brings an ink."""

    def setUp(self):
        self.screen = FakeScreen()

    def test_the_accent_bar_is_the_caller_s_ink_or_the_palette_accent(self):
        img = page(self.screen)
        panel.card(img, self.screen, "hello", accent=True)
        self.assertEqual(img.getpixel((960, panel.BAR - 1)), theme.rgb("accent"))
        img = page(self.screen)
        panel.card(img, self.screen, "hello", accent=(1, 2, 3))
        self.assertEqual(img.getpixel((960, panel.BAR - 1)), (1, 2, 3))
        # No bar asked for, no bar: the top row stays the page.
        img = page(self.screen)
        panel.card(img, self.screen, "hello")
        self.assertEqual(img.getpixel((960, panel.BAR - 1)), theme.rgb("page"))

    def test_headline_body_and_tag_read_the_palette(self):
        img = page(self.screen)
        panel.card(img, self.screen, "HEADLINE", "the body line",
                   tag_text="INFO")
        self.assertGreater(count(img, theme.rgb("ink-strong")), 0)
        self.assertGreater(count(img, theme.rgb("muted-soft")), 0)
        self.assertGreater(count(img, theme.rgb("muted")), 0)

    def test_a_caller_ink_replaces_the_headline_and_the_tag_role(self):
        img = page(self.screen)
        panel.card(img, self.screen, "HEADLINE", ink=(4, 5, 6),
                   tag_text="WARN", tag_ink=(7, 8, 9))
        self.assertGreater(count(img, (4, 5, 6)), 0)
        self.assertGreater(count(img, (7, 8, 9)), 0)
        self.assertEqual(count(img, theme.rgb("ink-strong")), 0)

    def test_the_card_never_raises_and_never_returns_a_different_frame(self):
        img = page(self.screen)

        class Broken:
            @property
            def W(self):
                raise RuntimeError("no panel")

            H = 1080

        self.assertIs(panel.card(img, Broken(), "hello"), img)
        self.assertIsNone(panel.card(None, self.screen, "hello"))
        for bad in (None, "", 7, object()):
            with self.subTest(title=bad):
                self.assertIs(panel.card(img, self.screen, bad), img)

    def test_the_tag_sits_at_the_region_s_own_corner(self):
        img = page(self.screen)
        panel.card(img, self.screen, "hello", tag_text="CRITICAL",
                   tag_ink=(1, 2, 3))
        # Bottom-left inset by PAD, not wherever a view used to put it.
        band = img.crop((panel.PAD, self.screen.H - panel.TAG_Y,
                         self.screen.W, self.screen.H))
        self.assertGreater(count(band, (1, 2, 3)), 0)
        self.assertEqual(count(img.crop((0, 0, panel.PAD, self.screen.H)),
                               (1, 2, 3)), 0)


class MigratedCardViewsTest(unittest.TestCase):
    """The three card views draw through the layer now."""

    def source(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_none_of_them_owns_a_font_a_fit_or_a_drawing_primitive(self):
        for rel in MIGRATED:
            src = self.source(rel)
            with self.subTest(module=rel):
                self.assertNotIn("from PIL", src)
                self.assertNotIn("ImageDraw", src)
                self.assertNotIn("ImageFont", src)
                self.assertNotIn("def _font", src)
                self.assertNotIn("def _shrink_to_fit", src)
                self.assertNotIn("def _fits", src)
                self.assertNotIn("def _autofit", src)
                self.assertIn("from ui import panel", src)
                # The only colour left is the view's playlist accent, which
                # theme.ACCENT_SLOTS also carries (and sleep declared none).
                self.assertLessEqual(set(HEX_RE.findall(src)),
                                     {theme.accent(CARDS[rel])})


class NoticeCardTest(unittest.TestCase):
    """The notice keeps its verified behaviour: a severity bar and tag."""

    def test_severity_maps_onto_the_palette_roles(self):
        from renderers import notice
        self.assertEqual(notice.severity_ink("info"), theme.rgb("accent"))
        self.assertEqual(notice.severity_ink("warn"), theme.rgb("attention"))
        self.assertEqual(notice.severity_ink("critical"), theme.rgb("alert"))
        # An unknown word is info, never an exception on the panel.
        for word in (None, "", "melted", 7):
            with self.subTest(word=word):
                self.assertEqual(notice.severity_ink(word),
                                 theme.rgb("accent"))
        for role in notice.SEVERITY_ROLE.values():
            self.assertEqual(theme.rgb(role), theme._rgb(theme.css(role)))

    def test_the_severity_bar_is_the_role_colour(self):
        for severity, role in (("info", "accent"), ("warn", "attention"),
                               ("critical", "alert")):
            with self.subTest(severity=severity):
                screen = render("renderers.notice", {"title": "hello",
                                                     "severity": severity})
                self.assertTrue(screen.frames, "notice drew no frame")
                frame = screen.frames[0]
                self.assertEqual(frame.getpixel((960, panel.BAR - 1)),
                                 theme.rgb(role))

    def test_the_color_param_still_wins_and_the_body_is_drawn(self):
        screen = render("renderers.notice",
                        {"title": "hello", "body": "world", "severity": "warn",
                         "color": "#010203"})
        frame = screen.frames[0]
        self.assertEqual(frame.getpixel((960, panel.BAR - 1)), (1, 2, 3))
        self.assertGreater(count(frame, theme.rgb("muted-soft")), 0)

    def test_a_notice_without_a_title_still_says_something(self):
        screen = render("renderers.notice", {"title": "   "})
        self.assertTrue(screen.frames)
        self.assertGreater(
            count(screen.frames[0], theme.rgb("ink-strong")), 0)


class TextAndSleepCardTest(unittest.TestCase):
    """The two centred-message views keep their behaviour, from tokens."""

    def test_text_is_centred_on_the_panel_background(self):
        screen = render("renderers.text", {"text": "hello panel"})
        frame = screen.frames[0]
        ink = theme.rgb("ink-strong")
        self.assertEqual(frame.getpixel((0, 0)), theme.rgb("page"))
        # Centred: the message is inked on BOTH sides of the middle, so it
        # cannot have drifted to one edge of the panel.
        self.assertGreater(count(frame.crop((0, 0, 960, 1080)), ink), 0)
        self.assertGreater(count(frame.crop((960, 0, 1920, 1080)), ink), 0)

    def test_text_honours_an_explicit_size_and_background(self):
        small = render("renderers.text",
                       {"text": "hello", "size": 40, "background": "#101010"})
        big = render("renderers.text", {"text": "hello", "size": 120})
        inked = [count(frame, theme.rgb("ink-strong"))
                 for frame in (small.frames[0], big.frames[0])]
        self.assertGreater(inked[1], inked[0])
        self.assertEqual(small.frames[0].getpixel((0, 0)), (16, 16, 16))

    def test_text_draws_nothing_for_no_message(self):
        screen = render("renderers.text", {})
        self.assertEqual(screen.frames, [])

    def test_sleep_names_the_way_back_in_the_faint_role(self):
        screen = render("renderers.sleep", {})
        frame = screen.frames[0]
        self.assertEqual(frame.getpixel((0, 0)), theme.rgb("page"))
        self.assertGreater(count(frame, theme.rgb("faint")), 0)

    def test_sleep_takes_an_operator_hint_and_ink(self):
        screen = render("renderers.sleep", {"hint": "waking up",
                                            "color": "#0a0b0c"})
        self.assertGreater(count(screen.frames[0], (10, 11, 12)), 0)


if __name__ == "__main__":
    unittest.main()
