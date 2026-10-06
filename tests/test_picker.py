"""Tests for the picker renderer. No framebuffer or touch hardware needed.

The picker is drawn by litehtml from html-templates/picker.html, so these
tests also pin the thing that migration can silently break: a drawn tile
and its touch region must be the SAME four numbers. TileMarkupTest and
TilePixelsTest check that against real rendered pixels, not against the
geometry function that produced them.

Run from the repo root:  python3 -m pytest tests/test_picker.py -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import touch
from PIL import Image

from renderers import picker as pk
import _html_native
import _html_templates as templates
import _picker_tiles as tiles
from ui import tile as ui_tile

W, H = 1920, 1080
VIEWS = ["clock", "chat", "row", "stream", "activity", "options"]
PICKER_PY = os.path.join(os.path.dirname(__file__), os.pardir,
                         "renderers", "picker.py")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), os.pardir,
                             "html-templates", "picker.html")

native_built = unittest.skipUnless(
    any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, name))
        for name in _html_native.LIB_NAMES),
    "native library not built (tools/build_litehtml.sh)")


class FakeScreen:
    W, H = W, H

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return default

    def font_path(self, family="DejaVuSans-Bold"):
        return None


class CoerceViewsTest(unittest.TestCase):
    def test_default_six(self):
        self.assertEqual(pk.coerce_views({}), list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.coerce_views(None), list(pk.DEFAULT_VIEWS))

    def test_garbage_falls_back(self):
        self.assertEqual(pk.coerce_views({"views": "clock"}),
                         list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.coerce_views({"views": []}),
                         list(pk.DEFAULT_VIEWS))

    def test_skips_bad_entries(self):
        self.assertEqual(pk.coerce_views({"views": ["clock", "", "a/b",
                                                    None, 5, " chat "]}),
                         ["clock", "chat"])

    def test_capped_at_max_views(self):
        many = ["v%d" % i for i in range(30)]
        self.assertEqual(pk.coerce_views({"views": many}),
                         many[:pk.MAX_VIEWS])
        self.assertEqual(pk.MAX_VIEWS, 24)


class LiveViewsTest(unittest.TestCase):
    def test_filters_param_gated_and_broken(self):
        registry = {
            "clock": {"module": object(), "params": {}},
            "macbook": {"module": object(),
                          "params": {"title": {"type": "string"}}},
            "reload": {"module": object(),
                         "params": {"sha": {"type": "string",
                                              "required": True}}},
            "notice": {"module": object(),
                         "params": {"title": {"type": "string",
                                                "required": True}}},
            "broken": {"broken": "boom"},
            "a/b": {"module": object(), "params": {}},
        }
        self.assertEqual(pk.live_views(registry), ["clock", "macbook"])

    def test_garbage_falls_back(self):
        self.assertEqual(pk.live_views(None), list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.live_views({}), list(pk.DEFAULT_VIEWS))
        self.assertEqual(pk.live_views("nope"), list(pk.DEFAULT_VIEWS))


class RectTest(unittest.TestCase):
    def test_default_rect_leaves_strips_and_bar(self):
        self.assertEqual(pk.default_rect(1920, 1080), [160, 40, 1600, 860])

    def test_explicit_rect_kept(self):
        self.assertEqual(pk.coerce_rect({"rect": [10, 20, 300, 400]},
                                        W, H), [10, 20, 300, 400])

    def test_garbage_rect_falls_back(self):
        self.assertEqual(pk.coerce_rect({"rect": "nope"}, W, H),
                         pk.default_rect(W, H))
        self.assertEqual(pk.coerce_rect({"rect": [0, 0, -5, 0]}, W, H),
                         pk.default_rect(W, H))


class GeometryTest(unittest.TestCase):
    def test_six_tiles_inside_rect_no_overlap(self):
        rect = pk.default_rect(W, H)
        geo = pk.grid_geometry(rect, 6)
        self.assertEqual(len(geo), 6)
        seen = set()
        for x, y, cw, ch in geo:
            self.assertGreaterEqual(x, rect[0])
            self.assertGreaterEqual(y, rect[1])
            self.assertLessEqual(x + cw, rect[0] + rect[2])
            self.assertLessEqual(y + ch, rect[1] + rect[3])
            for px in (x, x + cw - 1):
                for py in (y, y + ch - 1):
                    self.assertNotIn((px, py), seen)
                    seen.add((px, py))

    def test_single_tile_is_one_column(self):
        geo = pk.grid_geometry(pk.default_rect(W, H), 1)
        self.assertEqual(len(geo), 1)


class RegionsTest(unittest.TestCase):
    def test_tiles_fire_select_view(self):
        regions = pk.picker_regions(W, H, VIEWS)
        self.assertEqual([r["id"] for r in regions],
                         ["view-%s" % v for v in VIEWS])
        for region, view in zip(regions, VIEWS):
            method, path, body = touch.action_request(region["action"])
            self.assertEqual((method, path), ("POST", "/show"))
            self.assertEqual(body, {"renderer": view, "params": {}})

    def test_generator_matches_cli(self):
        # The touch.json procedure shells out to the module: the printed
        # regions must equal the library call with the same arguments.
        proc = subprocess.run(
            [sys.executable, "renderers/picker.py",
             "--width", str(W), "--height", str(H),
             "--views", ",".join(VIEWS)],
            capture_output=True, text=True, cwd=os.path.join(
                os.path.dirname(__file__), os.pardir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         pk.picker_regions(W, H, VIEWS))

    def test_shipped_example_matches_module(self):
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "touch-picker.json.example")
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["width"], 1920)
        self.assertEqual(doc["height"], 1080)
        self.assertEqual(doc["tap_options"]["renderer"], "picker")
        tiles = [r for r in doc["view_regions"]["picker"]
                 if r["id"].startswith("view-")]
        views = [r["action"]["view"] for r in tiles]
        # The example is generated from the live set, not hand-written:
        # it must still expose the views this fix is for.
        self.assertIn("macbook", views)
        self.assertEqual([r["id"] for r in tiles],
                         ["view-%s" % v for v in views])
        self.assertEqual(tiles, pk.picker_regions(
            1920, 1080, views, rect=pk.default_rect(1920, 1080)))
        for region, view in zip(tiles, views):
            method, path_, body = touch.action_request(region["action"])
            self.assertEqual((method, path_), ("POST", "/show"))
            self.assertEqual(body, {"renderer": view, "params": {}})
        # No view tiles leak into the global set (they would collide
        # with the macbook map sharing that space); scoped entries go
        # first at tap time, ahead of global chrome.
        self.assertTrue(all(not r["id"].startswith("view-")
                            for r in doc["regions"]))
        ordered = (doc["view_regions"]["picker"] + doc["regions"])
        self.assertTrue(all(r["id"].startswith("view-")
                            for r in ordered[:len(tiles)]))
        # The example itself validates (minus its comment key).
        cfg = dict(doc)
        cfg.pop("_comment", None)
        for region in cfg["regions"]:
            region.pop("_comment", None)
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(cfg, fh)
            tmp = fh.name
        try:
            loaded = touch.load_config(tmp)
        finally:
            os.unlink(tmp)
        self.assertTrue(loaded["tap_options"]["enabled"])


class DrawTest(unittest.TestCase):
    def test_run_presents_one_full_frame(self):
        screen = FakeScreen()
        stop = threading.Event()
        pk.run(screen, {"views": VIEWS}, stop)
        self.assertEqual(len(screen.frames), 1)
        self.assertEqual(screen.frames[0].size, (W, H))

    def test_run_survives_garbage_params(self):
        screen = FakeScreen()
        pk.run(screen, {"views": "nope", "rect": [0, 0, -1, -1],
                        "background": "nope"}, threading.Event())
        self.assertEqual(len(screen.frames), 1)


class SourceTest(unittest.TestCase):
    """The migration, stated as an invariant on the shipped source."""

    def source(self):
        with open(PICKER_PY, encoding="utf-8") as handle:
            return handle.read()

    def test_no_pillow_drawing_left_in_the_picker(self):
        # The oracle for this increment. A stray ImageDraw here means the
        # Pillow path is still live and the template path is decoration.
        self.assertNotIn("ImageDraw", self.source())
        self.assertNotIn("ImageFont", self.source())

    def test_the_picker_renders_through_the_template(self):
        source = self.source()
        self.assertIn(pk.TEMPLATE, source)
        self.assertTrue(os.path.isfile(TEMPLATE_PATH),
                        "html-templates/picker.html is the shipped surface")

    def test_the_shipped_template_matches_the_rendered_variables(self):
        # The template and chrome() cannot drift: every placeholder the
        # document declares is a key chrome() fills, and vice versa. Read
        # through the composition step, so the chrome the template splices
        # in is part of what is pinned.
        text = templates.source("picker.html",
                                os.path.dirname(TEMPLATE_PATH))
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        declared = set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)",
                                  text))
        self.assertIn("tiles", declared)
        supplied = set(tiles.chrome(FakeScreen(), pk.default_rect(W, H),
                                    VIEWS, (8, 10, 16), (255, 255, 255)))
        self.assertEqual(declared - {"tiles"}, supplied)
        self.assertEqual(supplied - declared, set())
        self.assertEqual(text.count("|raw"), 1)


class TileMarkupTest(unittest.TestCase):
    """Pure, no engine: the markup the picker hands the document."""

    def rects(self):
        return pk.grid_geometry(pk.default_rect(W, H), len(VIEWS))

    def test_one_tile_per_view_at_the_region_rect(self):
        markup = ui_tile.layer(VIEWS, self.rects(), pk.PALETTE, shadow=True)
        self.assertEqual(markup.count('class="tile"'), len(VIEWS))
        for (name, (x, y, cw, ch)) in zip(VIEWS, self.rects()):
            self.assertIn("left:%dpx; top:%dpx" % (x, y), markup)
            self.assertIn(name, markup)

    def test_a_tile_is_never_wider_than_its_touch_region(self):
        # litehtml puts the border outside the declared width: a tile that
        # forgot the compensation is 12px wider than the region that taps
        # it, on every side.
        for x, y, cw, ch in self.rects():
            icw, ich = ui_tile.content_size((x, y, cw, ch))
            self.assertEqual(cw - icw, 2 * ui_tile.BORDER)
            self.assertEqual(ch - ich, 2 * ui_tile.BORDER)
            self.assertGreater(icw, 0)
            self.assertGreater(ich, 0)

    def test_view_names_are_escaped_into_the_markup(self):
        # The one untrusted string in the tile layer is the name, and the
        # markup only exists because this escapes it first.
        markup = ui_tile.layer(["<script>x</script>"],
                               [(0, 0, 400, 200)], pk.PALETTE)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_label_shrinks_to_fit_a_narrow_tile(self):
        wide = ui_tile.fit_size("touch_confidence", 508)
        narrow = ui_tile.fit_size("touch_confidence", 120)
        self.assertGreater(wide, narrow)
        self.assertGreaterEqual(narrow, 12)

    def test_chrome_collapses_bands_a_tight_rect_has_no_room_for(self):
        roomy = tiles.chrome(FakeScreen(), pk.default_rect(W, H), VIEWS,
                             (8, 10, 16), (255, 255, 255))
        self.assertEqual(roomy["hint_left"], "ON")
        self.assertEqual(roomy["hint_right"], "NEXT")
        tight = tiles.chrome(FakeScreen(), [0, 0, W, H], VIEWS,
                             (8, 10, 16), (255, 255, 255))
        self.assertEqual(tight["title"], "")
        self.assertEqual(tight["hint_left"], "")
        self.assertEqual(tight["hint_right"], "")
        self.assertEqual(tight["footer"], "")


@native_built
class TilePixelsTest(unittest.TestCase):
    """What the panel actually shows, against what a tap would hit."""

    def frame(self, views=VIEWS):
        rect = pk.default_rect(W, H)
        geometry = pk.grid_geometry(rect, len(views))
        return pk.draw(FakeScreen(), views, geometry, rect, pk.PALETTE,
                       (8, 10, 16), (255, 255, 255), (140, 160, 190))

    def test_every_region_lands_on_its_own_coloured_tile(self):
        # Before the migration this could not hold: the Pillow path drew
        # a rectangle at the same numbers but nothing tied the two.
        frame = self.frame()
        regions = pk.picker_regions(W, H, VIEWS)
        for index, region in enumerate(regions):
            x, y, w, h = region["rect"]
            fill = pk.PALETTE[index % len(pk.PALETTE)]
            with self.subTest(tile=region["id"]):
                # inside the tile: the palette colour, never the panel
                self.assertEqual(frame.getpixel((x + 24, y + 24)), fill)
                # and the tile does not spill outside its own region
                self.assertNotEqual(frame.getpixel((x - 2, y + 24)), fill)
                self.assertNotEqual(frame.getpixel((x + w + 2, y + 24)), fill)
                self.assertNotEqual(frame.getpixel((x + 24, y + h + 2)), fill)

    def test_the_label_is_drawn_inside_its_tile(self):
        frame = self.frame()
        for region, (x, y, w, h) in zip(pk.picker_regions(W, H, VIEWS),
                                        pk.grid_geometry(
                                            pk.default_rect(W, H),
                                            len(VIEWS))):
            band = frame.crop((x + 8, y + h // 2 - 30, x + w - 8,
                               y + h // 2 + 30)).convert("L")
            darkest = min(band.getdata())
            with self.subTest(tile=region["id"]):
                # dark label ink on a bright tile, not a blank rectangle
                self.assertLess(darkest, 80)

    def test_the_chrome_bands_are_drawn(self):
        frame = self.frame()

        def lit(box):
            return max(sum(pixel)
                       for pixel in frame.crop(box).convert("RGB").getdata())

        self.assertGreater(lit((700, 870, 1220, 950)), 600)   # PICK A VIEW
        self.assertGreater(lit((40, 460, 130, 515)), 200)     # ON
        self.assertGreater(lit((1790, 460, 1880, 515)), 200)  # NEXT

    def test_a_custom_background_param_reaches_the_panel(self):
        rect = pk.default_rect(W, H)
        geometry = pk.grid_geometry(rect, len(VIEWS))
        frame = pk.draw(FakeScreen(), VIEWS, geometry, rect, pk.PALETTE,
                        (30, 0, 40), (255, 255, 255), (140, 160, 190))
        # the gutter between two tiles is bare document, not a hardcoded
        # panel colour: the background param really is honoured
        self.assertEqual(frame.getpixel((170, 150)), (30, 0, 40))

    def test_a_broken_template_is_a_card_not_a_blank(self):
        rect = pk.default_rect(W, H)
        geometry = pk.grid_geometry(rect, len(VIEWS))
        saved = pk.TEMPLATE
        pk.TEMPLATE = "no_such_template.html"
        try:
            frame = pk.draw(FakeScreen(), VIEWS, geometry, rect, pk.PALETTE,
                            (8, 10, 16), (255, 255, 255), (140, 160, 190))
        finally:
            pk.TEMPLATE = saved
        self.assertEqual(frame.size, (W, H))
        self.assertEqual(frame.getpixel((W // 2, 1)), (214, 74, 74))


@native_built
class ApplicationBandTest(unittest.TestCase):
    """The 15-70-15 side band: one usable application column.

    A fixed three columns is a 69px tile here -- a label no wider than
    the gutter around it. The column count is read off the region's
    shape (`ui.grid.columns`), and an explicit `cols` param still wins.
    """

    class BandScreen(FakeScreen):
        W, H = 288, H

    def render(self, params):
        screen = self.BandScreen()
        views = pk.coerce_views(params)
        rect = pk.coerce_rect(params, screen.W, screen.H)
        geometry = pk.grid_geometry(rect, len(views),
                                    cols=pk.coerce_cols(params))
        frame = pk.draw(screen, views, geometry, rect, pk.PALETTE,
                        (8, 10, 16), (255, 255, 255), (140, 160, 190))
        return frame, rect, geometry

    def test_the_band_is_one_column_of_wide_tiles(self):
        _frame, _rect, geometry = self.render({"views": VIEWS})
        self.assertEqual(len(geometry), len(VIEWS))
        self.assertEqual(len({x for x, _y, _w, _h in geometry}), 1)
        self.assertEqual(len({y for _x, y, _w, _h in geometry}), len(VIEWS),
                         "the band stacks down, it does not reflow across")
        for _x, _y, w, _h in geometry:
            self.assertGreaterEqual(w, 150, "a label needs room to be read")

    def test_the_drawn_tile_and_its_tap_region_are_the_same_numbers(self):
        frame, rect, geometry = self.render({"views": VIEWS})
        regions = pk.picker_regions(288, H, VIEWS, rect)
        self.assertEqual([e["rect"] for e in regions],
                         [list(r) for r in geometry])
        for index, region in enumerate(regions):
            x, y, _w, _h = region["rect"]
            with self.subTest(tile=region["id"]):
                self.assertEqual(frame.getpixel((x + 24, y + 24)),
                                 pk.PALETTE[index % len(pk.PALETTE)])

    def test_an_explicit_cols_param_overrides_the_shape(self):
        _frame, _rect, grid = self.render({"views": VIEWS, "cols": 3})
        self.assertEqual(len({x for x, _y, _w, _h in grid}), 3)
        self.assertLess(grid[0][2], 100)

    def test_a_garbage_cols_param_falls_back_to_the_shape(self):
        self.assertIsNone(pk.coerce_cols({"cols": "wide"}))
        self.assertIsNone(pk.coerce_cols({"cols": 0}))
        self.assertIsNone(pk.coerce_cols({"cols": True}))
        self.assertIsNone(pk.coerce_cols(None))
        self.assertEqual(pk.coerce_cols({"cols": 2}), 2)
        _frame, _rect, grid = self.render({"views": VIEWS, "cols": "wide"})
        self.assertEqual(len({x for x, _y, _w, _h in grid}), 1)

    def test_the_cols_param_is_advertised_and_honoured_by_run(self):
        self.assertIn("cols", pk.PARAMS)
        screen = self.BandScreen()
        stop = threading.Event()
        stop.set()
        pk.run(screen, {"views": VIEWS, "cols": 1}, stop)
        frame = screen.frames[-1]
        rect = pk.coerce_rect({"views": VIEWS}, screen.W, screen.H)
        grid = pk.grid_geometry(rect, len(VIEWS), cols=1)
        self.assertEqual(len({x for x, _y, _w, _h in grid}), 1)
        self.assertEqual(frame.getpixel((grid[0][0] + 24, grid[0][1] + 24)),
                         pk.PALETTE[0])


if __name__ == "__main__":
    unittest.main()
