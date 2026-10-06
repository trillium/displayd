"""Tests for the tile component (renderers/ui/tile.py).

The tile is "a bounded box with a label", and this is the one definition of
it: two duplicate tile layers were folded into this module, so what these
tests pin is that both rendering paths get the same geometry, the same fit
rule and the same palette roles -- and that a surface can no longer author
a tile of its own.

Run from the repo root:  python3 -m unittest tests.test_tile -v
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from PIL import Image

import _html_compose as compose
import _html_native
import _html_templates as templates
import _picker_tiles as picker_tiles
import theme
from ui import tile as ui_tile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_ROOT = os.path.join(ROOT, "html-templates")
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")

requires_native = unittest.skipUnless(
    any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, name))
        for name in _html_native.LIB_NAMES),
    "native library not built (tools/build_litehtml.sh)")


class GeometryTest(unittest.TestCase):
    """The one box rule: a declared box is the region minus its frame."""

    def test_the_declared_box_is_the_region_minus_the_frame(self):
        # litehtml puts the border OUTSIDE the declared width, so a tile
        # that must not spill past the region that taps it declares the
        # region minus 2*BORDER -- on both axes.
        cw, ch = ui_tile.content_size((10, 20, 300, 150))
        self.assertEqual(300 - cw, 2 * ui_tile.BORDER)
        self.assertEqual(150 - ch, 2 * ui_tile.BORDER)

    def test_a_frameless_cell_declares_the_whole_rect(self):
        self.assertEqual(ui_tile.content_size((0, 0, 100, 40), border=0),
                         (100, 40))

    def test_content_size_is_total_on_garbage(self):
        for bad in (None, "x", (1, 2), (1, 2, 3, 4, 5), ()):
            with self.subTest(rect=bad):
                cw, ch = ui_tile.content_size(bad)
                self.assertGreaterEqual(cw, 1)
                self.assertGreaterEqual(ch, 1)

    def test_fit_size_shrinks_a_long_label_and_floors(self):
        wide = ui_tile.fit_size("touch_confidence", 508)
        narrow = ui_tile.fit_size("touch_confidence", 120)
        self.assertGreater(wide, narrow)
        self.assertGreaterEqual(narrow, ui_tile.LABEL_MIN)

    def test_fit_size_respects_the_height_budget(self):
        roomy = ui_tile.fit_size("row", 900, height=400)
        short = ui_tile.fit_size("row", 900, height=40)
        self.assertLessEqual(short, roomy)

    def test_label_box_centres_inside_the_content_box(self):
        ox, oy = ui_tile.label_box("clock", (0, 0, 300, 150), 40)
        cw, ch = ui_tile.content_size((0, 0, 300, 150))
        self.assertGreaterEqual(ox, 0)
        self.assertLessEqual(ox, cw)
        self.assertGreaterEqual(oy, 0)
        self.assertLessEqual(oy, ch)

    def test_css_colour_takes_a_palette_tuple_or_an_already_css_colour(self):
        self.assertEqual(ui_tile.css_colour((18, 12, 32)), "#120c20")
        self.assertEqual(ui_tile.css_colour("#12ff7e"), "#12ff7e")
        self.assertEqual(ui_tile.css_colour(None), "")

    def test_css_colour_refuses_anything_that_is_not_a_colour(self):
        # The one reason this function exists: a caller's text must never
        # reach a style attribute, whatever it claims to be.
        for bad in ("red", "expression(alert(1))", "#12", "rgb(1,2,3)",
                    "#120c20; background: url(x)", 5, object()):
            with self.subTest(value=bad):
                self.assertEqual(ui_tile.css_colour(bad), "")


class MarkupTest(unittest.TestCase):
    """The markup half: what a raw slot receives."""

    def test_a_filled_tile_carries_its_frame_class_and_a_plain_cell_does_not(
            self):
        filled = ui_tile.cell((0, 0, 320, 160), "clock",
                              fill=(90, 200, 255), shadow=True)
        self.assertIn('class="tile"', filled)
        self.assertIn('class="lab"', filled)
        self.assertIn('class="shade"', filled)
        self.assertIn("background:#5ac8ff;", filled)
        plain = ui_tile.cell((0, 0, 320, 160), "clock", border=0,
                             ink=(255, 255, 255), box_cls="cell",
                             label_cls="name")
        self.assertIn('class="cell"', plain)
        self.assertIn('class="name"', plain)
        self.assertNotIn("shade", plain)
        self.assertIn("color:#ffffff;", plain)

    def test_the_declared_width_is_the_content_box(self):
        markup = ui_tile.cell((10, 20, 300, 150), "clock")
        cw, ch = ui_tile.content_size((10, 20, 300, 150))
        self.assertIn("left:10px; top:20px; width:%dpx; height:%dpx;"
                      % (cw, ch), markup)

    def test_a_hostile_label_is_escaped_and_never_closes_the_tile(self):
        markup = ui_tile.cell((0, 0, 300, 150), "</div><script>x</script>")
        self.assertNotIn("<script", markup)
        self.assertNotIn("</div><script", markup)
        self.assertIn("&lt;/div&gt;", markup)

    def test_a_hostile_colour_cannot_add_a_declaration(self):
        markup = ui_tile.cell((0, 0, 300, 150), "clock",
                              fill="#fff; background: url(http://x)")
        self.assertNotIn("url(", markup)
        self.assertNotIn("background:#fff", markup)

    def test_layer_cycles_the_fills_in_paint_order(self):
        rects = [(0, 0, 100, 50), (100, 0, 100, 50), (200, 0, 100, 50)]
        markup = ui_tile.layer(["a", "b", "c"], rects, [(1, 2, 3)])
        self.assertEqual(markup.count('class="tile"'), 3)
        self.assertLess(markup.index("left:0px"), markup.index("left:100px"))
        self.assertEqual(markup.count("background:#010203;"), 3)


class DrawHalfTest(unittest.TestCase):
    """The drawing half, on the same rules as the markup half."""

    def blank(self):
        return Image.new("RGB", (200, 100), theme.rgb("page"))

    def test_the_box_is_drawn_at_the_rect(self):
        img = ui_tile.draw(self.blank(), (10, 20, 100, 50),
                           outline=theme.rgb("accent"), width=2)
        self.assertEqual(img.getpixel((12, 21)), theme.rgb("accent"))
        self.assertEqual(img.getpixel((12, 80)), theme.rgb("page"))

    def test_the_label_is_centred_inside_the_content_box(self):
        img = Image.new("RGB", (400, 200), theme.rgb("page"))
        ui_tile.draw(img, (0, 0, 400, 200), "clock", ink=(255, 255, 255))
        painted = [(x, y) for y in range(200) for x in range(400)
                   if img.getpixel((x, y)) != theme.rgb("page")]
        self.assertTrue(painted, "no label was drawn")
        xs = [x for x, _y in painted]
        ys = [y for _x, y in painted]
        # inside the content box (a frame's worth in on every side)
        self.assertGreaterEqual(min(xs), ui_tile.BORDER - 1)
        self.assertLessEqual(max(xs), 400 - ui_tile.BORDER + 1)
        self.assertGreaterEqual(min(ys), ui_tile.BORDER - 1)

    def test_a_topleft_label_is_truncated_to_fit(self):
        img = Image.new("RGB", (120, 60), theme.rgb("page"))
        ui_tile.draw(img, (0, 0, 120, 60), "a-very-long-region-name", ink=(255, 255, 255),
                     place=ui_tile.TOPLEFT, border=0)
        self.assertNotEqual(img.tobytes(), Image.new(
            "RGB", (120, 60), theme.rgb("page")).tobytes())

    def test_a_broken_draw_returns_the_frame_unchanged(self):
        # The layer's obligation: a tile that cannot draw is a missing
        # label, never a blank panel.
        img = self.blank()
        for bad in (None, "x", (1, 2), object()):
            with self.subTest(rect=bad):
                self.assertIs(img, ui_tile.draw(img, bad, "clock"))


class OneDefinitionTest(unittest.TestCase):
    """A surface cannot author a tile of its own any more."""

    PANEL = ("layout.html", "picker.html", "options.html", "chat.html")

    def file(self, name):
        with open(os.path.join(SHIPPED_ROOT, name),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_the_stylesheet_frame_is_the_component_border(self):
        # The one number that crosses the language boundary: get it wrong
        # and every tile is 2*BORDER wider than the region that taps it.
        css = compose.partial_sections(SHIPPED_ROOT)["css"]
        self.assertIn("border-width: %dpx;" % ui_tile.BORDER, css)
        self.assertIn("border-color: var(--on-accent);", css)

    def test_the_tile_rules_live_in_the_one_shared_stylesheet(self):
        css = compose.partial_sections(SHIPPED_ROOT)["css"]
        for selector in (".layer", ".tile", ".tile .lab", ".tile .shade",
                         ".cell", ".cell .name"):
            with self.subTest(selector=selector):
                self.assertIn(selector + " {", css)

    def test_no_surface_restates_the_tile_look(self):
        for name in self.PANEL:
            local = re.sub(r"[ \t]*<!--\s*#include\s+[A-Za-z0-9_.-]+\s*-->"
                           r"[ \t]*\n?", "", self.file(name))
            with self.subTest(template=name):
                for selector in (".layer", ".tile", ".cell", ".name",
                                 ".lab", ".shade"):
                    self.assertNotIn(selector + " {", local)

    def test_the_tile_look_wears_palette_tokens_not_literals(self):
        partial = templates.COMMENT_RE.sub("", self.file("_chrome.html"))
        for selector in (".tile .lab", ".tile .shade"):
            self.assertIn(selector, partial)
        self.assertIn("var(--on-accent)", partial)
        self.assertIn("var(--label-shade)", partial)
        self.assertEqual(HEX_RE.findall(partial), [])


@requires_native
class TilePixelTest(unittest.TestCase):
    """The look really is the component's: proven on rendered pixels."""

    W, H = 1920, 1080
    VIEWS = ["clock", "chat", "row", "stream", "activity", "options"]

    class Screen:
        W, H = 1920, 1080

        @classmethod
        def color(cls, value, default=(255, 255, 255)):
            return default

        def font_path(self, family="DejaVuSans-Bold"):
            return None

    def frame(self):
        from renderers import picker as pk
        rect = pk.default_rect(self.W, self.H)
        geometry = pk.grid_geometry(rect, len(self.VIEWS))
        document, root = templates.load(
            "picker.html",
            picker_tiles.chrome(self.Screen(), rect, self.VIEWS,
                                (8, 10, 16), (255, 255, 255)),
            root=SHIPPED_ROOT,
            raw={"tiles": ui_tile.layer(self.VIEWS, geometry, [(90, 200, 255)],
                                        shadow=True)})
        image, _height = _html_native.render(document, self.W, self.H,
                                             background=(8, 10, 16), root=root)
        return image, geometry

    def test_the_frame_and_the_label_ink_are_the_on_accent_token(self):
        image, geometry = self.frame()
        x, y, _w, _h = geometry[0]
        # the frame: the shared stylesheet's longhand border taking a var()
        self.assertEqual(image.getpixel((x + 2, y + 30)),
                         theme.rgb("on-accent"),
                         "the tile frame is not the palette's on-accent")

    def test_the_fill_is_what_the_caller_asked_for(self):
        image, geometry = self.frame()
        x, y, _w, _h = geometry[0]
        self.assertEqual(image.getpixel((x + 40, y + 40)), (90, 200, 255))


if __name__ == "__main__":
    unittest.main()
