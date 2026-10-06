"""Tests for the design-token layer (renderers/theme.py).

The token layer has one job worth pinning: a colour in this repo has one
owner. So these tests are mostly structural -- the palette is complete and
deterministic, every template document inherits it, a migrated template
carries no colour literal, and the Pillow path reads the same roles -- plus
two pixel tests that prove the engine really resolves ``var(--token)``
rather than dropping the declaration.

Run from the repo root:  python3 -m unittest tests.test_theme -v
"""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import importlib.util

import displayd
import _html_compose as compose
import _html_native
import _html_templates as templates
import theme
from PIL import Image

REPO = os.path.dirname(os.path.abspath(displayd.__file__))
SHIPPED_ROOT = os.path.join(REPO, "html-templates")
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,6}\b")


def native_built():
    return any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, name))
               for name in _html_native.LIB_NAMES)


requires_native = unittest.skipUnless(
    native_built(), "native library not built (tools/build_litehtml.sh)")


def load_html():
    """The real html renderer module, for its DEFAULT_VARS contract."""
    path = os.path.join(displayd.RENDERER_DIR, "html.py")
    spec = importlib.util.spec_from_file_location("theme_test_html", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def colours(img):
    """The distinct colours in an image, exactly."""
    return set(c for _count, c in img.getcolors(maxcolors=1 << 20))


def write_template(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class PaletteTest(unittest.TestCase):
    """One module owns the palette: it must be readable and total."""

    def test_every_token_is_a_colour(self):
        for name, value in theme.TOKENS:
            with self.subTest(token=name):
                self.assertRegex(value, r"^#[0-9a-f]{6}$")
                self.assertEqual(len(theme.rgb(name)), 3)

    def test_token_names_are_unique(self):
        names = [name for name, _value in theme.TOKENS]
        self.assertEqual(len(names), len(set(names)))

    def test_css_root_is_deterministic_and_complete(self):
        block = theme.css_root()
        self.assertEqual(block, theme.css_root())
        for name, value in theme.TOKENS:
            self.assertIn("--%s: %s;" % (name, value), block)

    def test_unknown_token_is_an_error_not_a_default(self):
        # A typo must not silently hand a view the wrong colour.
        with self.assertRaises(KeyError):
            theme.rgb("no-such-role")
        with self.assertRaises(KeyError):
            theme.css("no-such-role")

    def test_rgb_is_the_one_conversion_point(self):
        self.assertEqual(theme.rgb("page"), (7, 8, 12))
        self.assertEqual(theme.rgb("badge"), (13, 17, 28))
        self.assertEqual(theme.rgb("ink-strong"), (255, 255, 255))

    def test_accent_slots_resolve_and_unknown_views_fall_back(self):
        self.assertEqual(theme.accent("clock"), theme.ACCENT_SLOTS["clock"])
        self.assertEqual(theme.accent("no-such-view"), theme.ACCENT)
        for view, value in theme.ACCENT_SLOTS.items():
            with self.subTest(view=view):
                self.assertRegex(value, r"^#[0-9a-fA-F]{6}$")
                self.assertEqual(len(theme.accent_rgb(view)), 3)

    def test_slot_root_names_are_css_safe(self):
        block = theme.slot_root()
        for view in theme.ACCENT_SLOTS:
            self.assertIn("--accent-%s:" % view.replace("_", "-"), block)
        self.assertNotIn("_", block)


class InjectionTest(unittest.TestCase):
    """Every panel document inherits the tokens, wherever it starts."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = _html_native.allow_root(self.dir.name)

    def load(self, name, variables=None):
        return templates.load(name, variables, root=self.root)

    def test_a_loaded_template_carries_one_token_block(self):
        write_template(self.dir.name, "t.html",
                       "<html><head></head><body><p>{{a}}</p></body></html>")
        text, _ = self.load("t.html", {"a": "x"})
        self.assertEqual(text.count(compose.MARKER), 1)
        self.assertEqual(text.count("--page: %s;" % theme.PAGE), 1)
        self.assertEqual(text.count("<style"), 1)

    def test_the_block_lands_inside_the_head(self):
        write_template(self.dir.name, "t.html",
                       "<html><head><title>T</title></head><body></body></html>")
        text, _ = self.load("t.html")
        self.assertLess(text.index("--page:"), text.index("</head>"))

    def test_a_headless_fragment_still_gets_the_tokens(self):
        write_template(self.dir.name, "frag.html", "<p>{{a}}</p>")
        text, _ = self.load("frag.html", {"a": "x"})
        self.assertTrue(text.startswith("<style>"))
        self.assertTrue(text.endswith("<p>x</p>"))

    def test_strip_tokens_gives_the_substitution_back_exactly(self):
        write_template(self.dir.name, "t.html", "<p>{{a}}|{{b}}</p>")
        text, _ = self.load("t.html", {"a": "1", "b": "<2>"})
        self.assertEqual(compose.strip_tokens(text), "<p>1|&lt;2&gt;</p>")

    def test_compose_is_idempotent(self):
        once = compose.compose("<html><head></head><body></body></html>")
        self.assertEqual(compose.compose(once), once)

    def test_compose_tolerates_non_text(self):
        self.assertIsNone(compose.compose(None))

    def test_every_shipped_template_round_trips(self):
        names = templates.available(SHIPPED_ROOT)
        self.assertIn("layout.html", names)
        self.assertGreaterEqual(len(names), 6)
        for name in names:
            with open(os.path.join(SHIPPED_ROOT, name),
                      encoding="utf-8") as handle:
                raw = handle.read()
            with self.subTest(template=name):
                composed = compose.compose(raw)
                self.assertEqual(composed.count(compose.MARKER), 1)
                self.assertEqual(compose.strip_tokens(composed), raw)


class MigratedTemplateTest(unittest.TestCase):
    """A migrated template styles from tokens, never from a literal."""

    MIGRATED = ("layout.html", "status.html")

    def test_no_colour_literal_in_a_migrated_style(self):
        for name in self.MIGRATED:
            with open(os.path.join(SHIPPED_ROOT, name),
                      encoding="utf-8") as handle:
                text = templates.COMMENT_RE.sub("", handle.read())
            with self.subTest(template=name):
                self.assertEqual(HEX_RE.findall(text), [],
                                 "%s re-authors a colour the palette owns"
                                 % name)
                self.assertIn("var(--", text)


@requires_native
class TokenPixelTest(unittest.TestCase):
    """The engine really resolves the variables -- proven, not assumed."""

    DOC = """<html><head><style>
      html, body { background: var(--page); }
      .good { width: 100px; height: 60px; background: var(--accent); }
      .bad  { width: 100px; height: 60px; background: var(--no-such-token); }
      </style></head><body><div class="good"></div><div class="bad"></div>
      </body></html>"""

    def render(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = _html_native.allow_root(directory.name)
        write_template(directory.name, "t.html", text)
        document, root = templates.load("t.html", root=root)
        image, _height = _html_native.render(document, 200, 140, root=root)
        return image

    def test_a_var_resolves_to_its_token_colour(self):
        image = self.render(self.DOC)
        self.assertEqual(image.getpixel((50, 30)), theme.rgb("accent"),
                         "var(--accent) did not resolve")

    def test_an_undefined_var_drops_the_declaration(self):
        # The negative control: the same document's second box asks for a
        # token that does not exist, so litehtml drops that declaration and
        # the page shows through. Without this, the test above would pass
        # on a panel that ignores var() entirely.
        image = self.render(self.DOC)
        self.assertEqual(image.getpixel((50, 90)), theme.rgb("page"))

    def test_the_shared_chrome_wears_the_palette(self):
        html = load_html()
        document, root = templates.load("layout.html", html.DEFAULT_VARS,
                                        root=SHIPPED_ROOT)
        image, _height = _html_native.render(document, 1920, 1080, root=root)
        counts = dict((colour, count)
                      for count, colour in image.getcolors(maxcolors=1 << 24))
        for token, least in (("band", 250000), ("page", 1000000),
                             ("rule", 4000), ("accent", 500),
                             ("faint", 500)):
            with self.subTest(token=token):
                self.assertGreater(counts.get(theme.rgb(token), 0), least,
                                   "%s is not on the panel as the token" % token)

    def test_a_view_accent_slot_is_rendered(self):
        image = self.render(self.DOC.replace("var(--accent)",
                                             "var(--accent-clock)"))
        self.assertEqual(image.getpixel((50, 30)), theme.accent_rgb("clock"))


class PillowConsumerTest(unittest.TestCase):
    """The path that has not migrated still reads the palette."""

    BADGE = Image.new("RGB", (1920, 1080), (0, 0, 0))

    def test_both_system_buttons_draw_the_badge_token(self):
        from ui import system_buttons as buttons
        for name, draw, box in (("home", buttons.draw_home_button,
                                 (0, 0, 160, 160)),
                                ("sleep", buttons.draw_sleep_button,
                                 (1760, 0, 1920, 160))):
            with self.subTest(button=name):
                img = draw(self.BADGE.copy())
                painted = colours(img.crop(box))
                self.assertIn(theme.rgb("badge"), painted)
                self.assertIn(theme.rgb("ink-strong"), painted)
                self.assertEqual(painted - {theme.rgb("badge"),
                                            theme.rgb("ink-strong"),
                                            (0, 0, 0)}, set())

    def test_the_failure_card_draws_the_alert_family(self):
        import _html_error

        class Screen:
            W, H = 640, 360

            def new_image(self, background=(0, 0, 0)):
                return Image.new("RGB", (self.W, self.H), background)

            def color(self, value, default=(255, 255, 255)):
                return default

        frame = _html_error.error_frame(Screen(), "html: boom", "fix it")
        painted = colours(frame)
        self.assertIn(theme.rgb("alert"), painted)
        self.assertIn(theme.rgb("alert-ink"), painted)
        self.assertEqual(frame.getpixel((320, 1)), theme.rgb("alert"))


if __name__ == "__main__":
    unittest.main()
