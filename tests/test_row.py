"""Tests for the row (Concept2 streak) wall view.

Streak math mirrors row.sh's rest-day bank rule; these tests pin the
edge cases: covered rest day, streak break on an empty bank, empty log,
garbage lines, year reset, and future rows. Rendering tests use a
fontless FakeScreen (CI has no fonts) and only assert nonblank frames.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import datetime
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "renderers"))

import displayd

from PIL import Image

import row as row_view


D = datetime.date


def counts_from(days):
    """{date: rows} helper: days is [(iso, n), ...]."""
    out = {}
    for iso, n in days:
        out[D.fromisoformat(iso)] = n
    return out


def log_text(days):
    """rows.txt-style text for [(iso, n), ...]."""
    lines = []
    for iso, n in days:
        for i in range(n):
            lines.append("%sT12:00:%02d-08:00" % (iso, i))
    return "\n".join(lines) + "\n"


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
        return None  # CI box has no fonts: renderers must survive that


def _nonblank(img):
    extrema = img.getextrema()
    return any(lo != hi for lo, hi in extrema)


class TestParseLog(unittest.TestCase):
    def test_counts_rows_per_day(self):
        counts, last, total = row_view.parse_log(log_text([("2026-01-01", 2), ("2026-01-02", 1)]))
        self.assertEqual(counts, {D(2026, 1, 1): 2, D(2026, 1, 2): 1})
        self.assertEqual(total, 3)
        self.assertTrue(last.startswith("2026-01-02"))

    def test_garbage_lines_ignored(self):
        text = "??\n\n   \n" + log_text([("2026-01-01", 1)]) + "not-a-date\n2026-13-99T99:99:99-08:00\n"
        counts, last, total = row_view.parse_log(text)
        self.assertEqual(counts, {D(2026, 1, 1): 1})
        self.assertEqual(total, 1)

    def test_empty_log(self):
        self.assertEqual(row_view.parse_log(""), ({}, None, 0))
        self.assertEqual(row_view.parse_log("??\n??\n"), ({}, None, 0))


class TestStreakMath(unittest.TestCase):
    def test_consecutive_days(self):
        c = counts_from([("2026-01-01", 1), ("2026-01-02", 1), ("2026-01-03", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 3)), (3, 3, 0))

    def test_extra_rows_build_bank(self):
        c = counts_from([("2026-01-01", 3)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 1)), (1, 3, 2))

    def test_covered_rest_day_holds(self):
        # Bank of 1 covers the missed 01-02: day streak grows, rows unchanged.
        c = counts_from([("2026-01-01", 2), ("2026-01-03", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 3)), (3, 3, 0))

    def test_rest_day_without_bank_breaks(self):
        c = counts_from([("2026-01-01", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 2)), (0, 0, 0))

    def test_two_misses_on_bank_of_one_breaks(self):
        c = counts_from([("2026-01-01", 2)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 3)), (0, 0, 0))

    def test_fresh_start_after_break(self):
        c = counts_from([("2026-01-01", 1), ("2026-01-05", 2)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 5)), (1, 2, 1))

    def test_empty_log_is_zero(self):
        self.assertEqual(row_view.compute_streaks({}, D(2026, 1, 5)), (0, 0, 0))

    def test_future_rows_ignored(self):
        c = counts_from([("2026-01-01", 1), ("2026-06-01", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 1)), (1, 1, 0))

    def test_year_boundary_resets(self):
        c = counts_from([("2025-12-31", 2), ("2026-01-01", 1)])
        ds, rs, bank = row_view.compute_streaks(c, D(2026, 1, 1))
        self.assertEqual((ds, rs, bank), (1, 1, 0))

    def test_bank_invariant(self):
        # bank == rows - days whenever the streak is alive.
        c = counts_from([("2026-01-01", 2), ("2026-01-03", 3), ("2026-01-04", 1)])
        ds, rs, bank = row_view.compute_streaks(c, D(2026, 1, 4))
        self.assertGreater(ds, 0)
        self.assertEqual(bank, rs - ds)


class TestSummarize(unittest.TestCase):
    def test_rowed_today(self):
        c = counts_from([("2026-01-01", 1), ("2026-01-02", 1)])
        s = row_view.summarize(c, "2026-01-02T12:00:00-08:00", 2, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "rowed today")
        self.assertEqual(s["day_streak"], 2)
        self.assertEqual(s["rows_year"], 2)
        self.assertEqual(s["pace"], 2 - 2)

    def test_rest_day_held(self):
        c = counts_from([("2026-01-01", 2)])
        s = row_view.summarize(c, "2026-01-01T12:00:00-08:00", 2, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "rest — streak held")
        self.assertEqual(s["day_streak"], 2)
        self.assertEqual(s["row_streak"], 2)

    def test_broken(self):
        c = counts_from([("2026-01-01", 1)])
        s = row_view.summarize(c, "2026-01-01T12:00:00-08:00", 1, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "streak broken")
        self.assertEqual(s["day_streak"], 0)

    def test_empty(self):
        s = row_view.summarize({}, None, 0, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "no rows yet")
        self.assertEqual((s["day_streak"], s["row_streak"], s["bank"]), (0, 0, 0))


class TestPathResolution(unittest.TestCase):
    def test_explicit_param_wins(self):
        self.assertEqual(row_view.resolve_path("/tmp/custom-rows.txt"), "/tmp/custom-rows.txt")

    def test_env_var_used(self):
        old = os.environ.get(row_view.ENV_VAR)
        os.environ[row_view.ENV_VAR] = "/tmp/env-rows.txt"
        try:
            self.assertEqual(row_view.resolve_path(), "/tmp/env-rows.txt")
        finally:
            if old is None:
                del os.environ[row_view.ENV_VAR]
            else:
                os.environ[row_view.ENV_VAR] = old

    def test_no_hardcoded_home_paths(self):
        with open(row_view.__file__) as fh:
            src = fh.read()
        for marker in ("/Users/", "/home/", "/root/", "$HOME", "expanduser"):
            self.assertNotIn(marker, src, "hardcoded home reference %r" % marker)


class TestContract(unittest.TestCase):
    def test_renderer_loads_and_advertises(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("row", found)
        self.assertIn("module", found["row"], found["row"].get("broken"))
        mod = found["row"]["module"]
        self.assertFalse(mod.STATIC)
        self.assertTrue(callable(mod.run))
        self.assertIn("path", mod.PARAMS)
        for pname, spec in mod.PARAMS.items():
            self.assertIn(spec.get("type"), ("string", "integer", "number", "boolean"))

    def test_missing_file_frame_is_not_blank(self):
        screen = FakeScreen()
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        row_view.run(screen, {"path": "/tmp/does-not-exist-rows.txt", "interval": 5}, stop)
        self.assertTrue(screen.frames, "missing log must still present a frame")
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_empty_log_frame_is_not_blank(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("??\n\n")
            name = fh.name
        try:
            screen = FakeScreen()
            stop = threading.Event()
            threading.Timer(0.2, stop.set).start()
            row_view.run(screen, {"path": name, "interval": 5}, stop)
            self.assertTrue(screen.frames)
            self.assertTrue(_nonblank(screen.frames[-1]))
        finally:
            os.unlink(name)

    def test_populated_log_renders_and_reports_streak(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(log_text([("2026-01-01", 2), ("2026-01-02", 1)]))
            name = fh.name
        try:
            snap = row_view.read_snapshot(name)
            # Dates are historical; only the shape matters, not liveness.
            self.assertEqual(snap["rows_year"], 3)
            screen = FakeScreen()
            stop = threading.Event()
            threading.Timer(0.2, stop.set).start()
            row_view.run(screen, {"path": name, "interval": 5}, stop)
            self.assertTrue(screen.frames)
            self.assertTrue(_nonblank(screen.frames[-1]))
            self.assertEqual(screen.frames[-1].size, (1920, 1080))
        finally:
            os.unlink(name)


if __name__ == "__main__":
    unittest.main()
