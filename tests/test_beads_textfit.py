"""Tests for the shared text-fitting helpers in beads_common (_fit/_wrap).

No framebuffer needed. Uses a deterministic FakeDraw (fixed advance per
glyph) for exact assertions, plus PIL's bundled default font for one
real-metrics integration check.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from PIL import Image, ImageDraw, ImageFont

from renderers import beads_common as common


class FakeFont:
    """Stand-in font: every glyph costs `advance` px (honoured by FakeDraw)."""

    def __init__(self, advance):
        self.advance = advance


class FakeDraw:
    """Deterministic textlength: len(text) * per-glyph advance."""

    def __init__(self, advance=10):
        self.advance = advance

    def textlength(self, text, font=None):
        adv = getattr(font, "advance", self.advance)
        return len(text) * adv


def real_draw():
    img = Image.new("RGB", (1920, 1080), (0, 0, 0))
    return ImageDraw.Draw(img), ImageFont.load_default()


class TestFit(unittest.TestCase):
    def test_fits_returns_unchanged(self):
        draw, font = FakeDraw(10), FakeFont(10)
        self.assertEqual(common._fit(draw, "hello", font, 200), "hello")
        # Exactly at budget: no gratuitous truncation either.
        self.assertEqual(common._fit(draw, "x" * 20, font, 200), "x" * 20)

    def test_long_string_shrinks_to_budget(self):
        draw, font = FakeDraw(10), FakeFont(10)
        out = common._fit(draw, "x" * 200, font, 100)
        self.assertTrue(out)
        self.assertLess(len(out), 200)
        self.assertLessEqual(draw.textlength(out, font=font), 100)

    def test_single_oversized_glyph(self):
        # REGRESSION (task-qbwly): a single glyph wider than max_w made the
        # old loop exit at len(text) == 4 and return an overflowing fragment.
        # _fit must never return text wider than max_w.
        draw, font = FakeDraw(100), FakeFont(100)
        out = common._fit(draw, "W", font, 50)
        self.assertLessEqual(draw.textlength(out, font=font), 50)
        out = common._fit(draw, "WWWW", font, 50)
        self.assertLessEqual(draw.textlength(out, font=font), 50)

    def test_empty_none_nonstring(self):
        # Contract: str(text or "") -- falsy inputs collapse to "".
        draw, font = FakeDraw(10), FakeFont(10)
        self.assertEqual(common._fit(draw, "", font, 100), "")
        self.assertEqual(common._fit(draw, None, font, 100), "")
        self.assertEqual(common._fit(draw, 0, font, 100), "")
        self.assertEqual(common._fit(draw, False, font, 100), "")
        self.assertEqual(common._fit(draw, 123, font, 1000), "123")

    def test_max_chars_respected_with_font(self):
        draw, font = FakeDraw(10), FakeFont(10)
        out = common._fit(draw, "y" * 200, font, 10_000, max_chars=10)
        self.assertLessEqual(len(out), 10)

    def test_max_chars_respected_without_font(self):
        draw = FakeDraw(10)
        out = common._fit(draw, "z" * 200, None, 10_000, max_chars=10)
        self.assertLessEqual(len(out), 10)
        self.assertEqual(out, "z" * 10)

    def test_no_font_fallback_ignores_pixel_width(self):
        # Deliberate, documented contract: with no font there are no metrics,
        # so _fit falls back to the character budget and ignores max_w.
        draw = FakeDraw(10)
        self.assertEqual(common._fit(draw, "hello", None, 1), "hello")

    def test_zero_or_negative_budget(self):
        draw, font = FakeDraw(10), FakeFont(10)
        self.assertEqual(common._fit(draw, "hello", font, 0), "")
        self.assertEqual(common._fit(draw, "hello", font, -5), "")

    def test_never_wider_than_budget_sweep(self):
        # Property: for any input, the result fits max_w (or is empty).
        cases = ["", "a", "hello world", "x" * 500, "W", "WWWW",
                 "mixed 123 !@# " * 20]
        for adv in (1, 7, 10, 100):
            draw, font = FakeDraw(adv), FakeFont(adv)
            for max_w in (0, 1, 5, 50, 333, 2000):
                with self.subTest(adv=adv, max_w=max_w):
                    for text in cases:
                        out = common._fit(draw, text, font, max_w)
                        self.assertLessEqual(
                            draw.textlength(out, font=font), max_w,
                            "adv=%r max_w=%r text=%r -> %r"
                            % (adv, max_w, text[:20], out))

    def test_real_font_integration(self):
        draw, font = real_draw()
        self.assertEqual(common._fit(draw, "hi", font, 500), "hi")
        out = common._fit(draw, "hello world " * 40, font, 200)
        self.assertTrue(out)
        self.assertLessEqual(draw.textlength(out, font=font), 200)


class TestReasonTitleComposition(unittest.TestCase):
    def test_fitted_reason_leaves_truthful_title_budget(self):
        # Mirrors the renderers/beads.py attention-row arithmetic: the reason
        # is capped to ~40% of the row first, then the title is fitted into
        # the measured remainder. If the reason overflows its budget, the
        # title width is computed from a lie and the row runs off the panel.
        draw, font = FakeDraw(10), FakeFont(10)
        width = 1776  # screen.W - 2*PAD - 24 on the 1920 panel
        tag = "[task task-abcdef] "
        reason = common._fit(draw, "waits on some very long reason " * 10,
                             font, width * 0.40, max_chars=56)
        self.assertLessEqual(draw.textlength(reason, font=font), width * 0.40)
        tail = " \u2014 " + reason
        fixed = (draw.textlength(tag, font=font)
                 + draw.textlength(tail, font=font))
        title_w = max(120, width - fixed)
        title = common._fit(draw, "some very long title " * 20, font, title_w)
        total = draw.textlength(tag + title + tail, font=font)
        self.assertLessEqual(total, width)


class TestWrap(unittest.TestCase):
    def test_short_string_single_row(self):
        draw, font = FakeDraw(10), FakeFont(10)
        self.assertEqual(common._wrap(draw, "hi there", font, 1000),
                         ["hi there"])

    def test_empty_string(self):
        draw, font = FakeDraw(10), FakeFont(10)
        self.assertEqual(common._wrap(draw, "", font, 100), [""])
        self.assertEqual(common._wrap(draw, None, font, 100), [""])

    def test_respects_max_rows(self):
        draw, font = FakeDraw(10), FakeFont(10)
        text = "word " * 100
        rows = common._wrap(draw, text, font, 100, max_rows=2)
        self.assertLessEqual(len(rows), 2)

    def test_truncation_marks_final_row_with_ellipsis(self):
        draw, font = FakeDraw(10), FakeFont(10)
        rows = common._wrap(draw, "word " * 100, font, 100, max_rows=2)
        self.assertEqual(len(rows), 2)
        self.assertIn("\u2026", rows[-1])
        for row in rows:
            self.assertLessEqual(draw.textlength(row, font=font), 100)

    def test_single_word_longer_than_width(self):
        # Must terminate with a non-empty result, never spin or vanish.
        draw, font = FakeDraw(10), FakeFont(10)
        rows = common._wrap(draw, "supercalifragilistic" * 10, font, 100,
                            max_rows=3)
        self.assertTrue(rows)
        self.assertTrue(any(rows))
        self.assertLessEqual(len(rows), 3)

    def test_oversized_glyphs_rows_fit(self):
        # Wrap-level proof of the _fit defect: even rows built from glyphs
        # wider than max_w must come back fitting (or empty), never wider.
        draw, font = FakeDraw(100), FakeFont(100)
        rows = common._wrap(draw, "aa bb cc dd ee", font, 50, max_rows=2)
        self.assertTrue(rows)
        for row in rows:
            self.assertLessEqual(draw.textlength(row, font=font), 50)

    def test_no_font_wrap_terminates(self):
        rows = common._wrap(FakeDraw(10), "alpha beta gamma delta", None,
                            100, max_rows=2)
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
