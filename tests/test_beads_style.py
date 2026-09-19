"""Tests for the beads overview store styles. No framebuffer needed."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from PIL import Image, ImageDraw, ImageFont

from renderers import beads
from renderers import beads_style as styles


class FakeScreen:
    W, H = 1920, 1080

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    def clear(self, background=(0, 0, 0)):
        self.present(self.new_image(background))

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return displayd.Screen.color(value, default)

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return displayd.Screen.font_path(family)


def raw(iid, status="open", deps=(), labels=None, priority=2):
    return {"id": iid, "title": "title " + iid, "status": status,
            "priority": priority, "labels": list(labels or []),
            "dependencies": [{"issue_id": iid, "depends_on_id": t, "type": k}
                             for k, t in deps]}


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


class TestDefaults(unittest.TestCase):
    def test_ships_stores_in_use(self):
        for store in ("task", "brain", "review", "robots", "ideas"):
            color, icon, known = styles.style_for(store, dict(
                styles.STORE_DEFAULTS))
            self.assertTrue(known, store)
            self.assertTrue(icon.strip(), store)
            self.assertEqual(len(color), 3, store)
            self.assertTrue(all(0 <= v <= 255 for v in color), store)

    def test_default_colours_distinguishable(self):
        colors = [styles.parse_color(v["color"], (0, 0, 0))
                  for v in styles.STORE_DEFAULTS.values()]
        for i in range(len(colors)):
            for j in range(i + 1, len(colors)):
                self.assertGreater(
                    _dist(colors[i], colors[j]), 50,
                    "store colours %d and %d too close: %r vs %r"
                    % (i, j, colors[i], colors[j]))


class TestUnknownStores(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(styles.unknown_color("quux"),
                         styles.unknown_color("quux"))
        # Stable across processes: hardcoded spot value.
        self.assertEqual(styles.unknown_color("quux"), (113, 103, 228))

    def test_unknowns_differ(self):
        colors = {styles.unknown_color(n) for n in
                  ("alpha", "beta", "gamma", "delta")}
        self.assertEqual(len(colors), 4)

    def test_unknown_gets_default_icon_never_blank(self):
        color, icon, known = styles.style_for("some-new-store", {})
        self.assertFalse(known)
        self.assertTrue(icon.strip(), "icon must never be blank")
        self.assertTrue(all(0 <= v <= 255 for v in color))

    def test_unknown_never_crashes(self):
        for bad in (None, "", "   ", 123):
            color, icon, _ = styles.style_for(bad, {})
            self.assertTrue(icon.strip())
            self.assertEqual(len(color), 3)


class TestConfigFile(unittest.TestCase):
    def setUp(self):
        styles.clear_cache()

    def tearDown(self):
        styles.clear_cache()

    def _write(self, obj, raw_text=None):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        fh.write(raw_text if raw_text is not None else json.dumps(obj))
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_override_without_code_change(self):
        path = self._write({"stores": {
            "garden": {"color": "#7DE3A8", "icon": "\u2698"}}})
        got, source, error = styles.load_store_styles(path)
        self.assertIsNone(error, error)
        self.assertEqual(source, path)
        color, icon, known = styles.style_for("garden", got)
        self.assertTrue(known)
        self.assertEqual(color, (125, 227, 168))
        self.assertEqual(icon, "\u2698")
        # Untouched stores keep their defaults.
        color, icon, known = styles.style_for("task", got)
        self.assertTrue(known)
        self.assertEqual(icon, "\u25cf")

    def test_malformed_file_keeps_builtins(self):
        path = self._write(None, raw_text="{not json,,,")
        got, source, error = styles.load_store_styles(path)
        self.assertIsNotNone(error)
        color, icon, known = styles.style_for("task", got)
        self.assertTrue(known)
        self.assertEqual(icon, "\u25cf")

    def test_missing_file_uses_builtins(self):
        got, source, error = styles.load_store_styles(
            "/nonexistent/beads-stores.json")
        self.assertIsNone(error, error)
        # The shipped defaults file (when present) is a fine source too;
        # the point is no crash and real defaults.
        self.assertTrue(source == "builtins"
                        or source.endswith("beads_stores.json"), source)
        _, icon, known = styles.style_for("review", got)
        self.assertTrue(known)
        self.assertEqual(icon, "\u2605")

    def test_bad_colour_falls_back_deterministically(self):
        path = self._write({"stores": {"task": {"color": "not-a-colour"}}})
        got, _, _ = styles.load_store_styles(path)
        color, _, _ = styles.style_for("task", got)
        self.assertEqual(color, styles.unknown_color("task"))

    def test_reload_on_mtime_change(self):
        path = self._write({"stores": {"task": {"color": "#000001"}}})
        got, _, _ = styles.load_store_styles(path)
        self.assertEqual(styles.style_for("task", got)[0], (0, 0, 1))
        import time as _t
        _t.sleep(1.05)  # mtime granularity
        with open(path, "w") as fh:
            json.dump({"stores": {"task": {"color": "#000002"}}}, fh)
        got2, _, _ = styles.load_store_styles(path)
        self.assertEqual(styles.style_for("task", got2)[0], (0, 0, 2))


class TestTagFormat(unittest.TestCase):
    def test_no_duplicated_store_prefix(self):
        self.assertEqual(
            styles.format_tag({"id": "review-a2n", "store": "review"}),
            "[review-a2n]")
        self.assertEqual(
            styles.format_tag({"id": "task-nh3y", "store": "task"}),
            "[task-nh3y]")


class TestOverviewDraw(unittest.TestCase):
    def setUp(self):
        styles.clear_cache()
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=None, updated=0.0, health="cold",
                               error=None, source=None)

    def tearDown(self):
        styles.clear_cache()
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=None, updated=0.0, health="cold",
                               error=None, source=None)

    def _draw_attention_texts(self, pairs):
        snap = beads._classify(pairs)
        import time as _t
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=snap, updated=_t.time(),
                               health="warm", source="test")
        seen = []
        orig_text = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *a, **k):
            seen.append(text)
            return orig_text(self, xy, text, *a, **k)

        ImageDraw.ImageDraw.text = spy
        try:
            screen = FakeScreen()
            beads._draw(screen, "BEADS", (8, 8, 12))
        finally:
            ImageDraw.ImageDraw.text = orig_text
        return screen, seen

    def test_attention_tag_has_no_store_prefix_reason_intact(self):
        _, seen = self._draw_attention_texts([
            ("review", raw("review-a2n", "open", priority=1)),
            ("task", raw("task-nh3y", "open",
                          deps=[("blocks", "task-ac7w")])),
            ("task", {"id": "task-ac7w", "title": "Gate: human",
                      "status": "open", "priority": 2, "labels": [],
                      "dependencies": []})])
        joined = "\n".join(seen)
        self.assertIn("[review-a2n]", joined)
        self.assertIn("[task-nh3y]", joined)
        self.assertNotIn("[review review-", joined)
        self.assertNotIn("[task task-", joined)
        self.assertIn("review queue", joined)
        self.assertIn("Gate: human", joined)

    def test_unknown_store_row_renders(self):
        screen, seen = self._draw_attention_texts([
            ("review", raw("review-a2n", "open", priority=1)),
            ("zephyr", raw("zephyr-q1", "open", priority=0,
                              labels=["human"]))])
        self.assertTrue(screen.frames)
        joined = "\n".join(seen)
        self.assertIn("[zephyr-q1]", joined)

    def test_malformed_store_config_still_draws(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        fh.write("{broken")
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        with beads._POLL["lock"]:
            beads._POLL["cfg"] = {"store_config": fh.name}
        screen, seen = self._draw_attention_texts([
            ("task", raw("task-nh3y", "open",
                          deps=[("blocks", "task-ac7w")])),
            ("task", {"id": "task-ac7w", "title": "Gate: human",
                      "status": "open", "priority": 2, "labels": [],
                      "dependencies": []})])
        self.assertTrue(screen.frames)
        self.assertIn("[task-nh3y]", "\n".join(seen))

    def test_buckets_progress_health_unchanged(self):
        screen, seen = self._draw_attention_texts([
            ("task", raw("a", "in_progress")),
            ("review", raw("r1", "open"))])
        joined = "\n".join(seen)
        for token in ("Rolling", "Lined Up", "Stalled", "Past the Stand",
                      "past the stand", "NEEDS THE CAPTAIN"):
            self.assertIn(token, joined, token)


def _find_dejavu():
    cands = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/nix/store/c2bnmddhcgmhfblk359r3jz83sfgh8vl-"
        "dejavu-fonts-minimal-2.37/share/fonts/truetype/DejaVuSans.ttf",
    ]
    for path in cands:
        if os.path.isfile(path):
            return path
    try:
        import matplotlib
        path = os.path.join(os.path.dirname(matplotlib.__file__),
                            "mpl-data", "fonts", "ttf", "DejaVuSans.ttf")
        if os.path.isfile(path):
            return path
        from matplotlib import font_manager
        return font_manager.findfont("DejaVu Sans")
    except Exception:
        return None


class TestIconGlyphs(unittest.TestCase):
    """Every configured icon must be a real DejaVu Sans glyph (cmap +
    ink on pixels), not a tofu box. Checked, not assumed."""

    def test_glyphs_present_with_ink(self):
        path = _find_dejavu()
        if not path or not os.path.isfile(path):
            self.skipTest("no DejaVu Sans available to check glyphs")
        from fontTools.ttLib import TTFont
        cmap = TTFont(path).getBestCmap()
        glyphs = {v["icon"] for v in styles.STORE_DEFAULTS.values()}
        glyphs.add(styles.DEFAULT_ICON)
        font = ImageFont.truetype(path, 40)
        for glyph in glyphs:
            self.assertEqual(len(glyph), 1, glyph)
            self.assertIn(ord(glyph), cmap,
                          "U+%04X missing from DejaVu Sans" % ord(glyph))
            img = Image.new("RGB", (60, 60), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.text((5, 5), glyph, font=font, fill=(255, 255, 255))
            ink = sum(1 for px in img.getdata() if px != (0, 0, 0))
            self.assertGreater(ink, 20,
                               "U+%04X renders blank" % ord(glyph))


if __name__ == "__main__":
    unittest.main()
