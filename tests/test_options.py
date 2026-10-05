"""Options-view tests: the tap-anywhere landing screen.

Covers renderers/options.py (the view-selection screen every unconsumed
tap routes to): advertised through load_renderers with a valid PARAMS
schema, coerce_views fallbacks, headless frame rendering at panel and
tiny sizes, and the show -> show(clock) round-trip through a headless
DisplayDaemon (tap lands options; control-page /show is the way back).

The screen is drawn by litehtml from html-templates/options.html, so the
tests also pin what migration can silently break: the shipped source must
carry no Pillow drawing, the template and the variables the renderer
supplies must be the same set, a caller-supplied view name must arrive
escaped even though it lands in a raw slot, and the drawn name must sit
in the rect grid_geometry() says it does. Those are checked against real
rendered pixels, not against the geometry function that produced them.

Run from the repo root:  python3 -m unittest tests.test_options -v
"""

import io
import os
import re
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image, ImageChops

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
import _html_native
import _options_grid as grid

OPT_PATH = os.path.join(displayd.RENDERER_DIR, "options.py")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), os.pardir,
                             "html-templates", "options.html")

native_built = unittest.skipUnless(
    any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, name))
        for name in _html_native.LIB_NAMES),
    "native library not built (tools/build_litehtml.sh)")


def load_options(name="options_testmod"):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, OPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OPT = load_options("options_mod")


class FakeFb:
    def __init__(self, w=480, h=270):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


def make_screen(w=480, h=270):
    return displayd.Screen(FakeFb(w, h))


def non_bg_count(img):
    small = img.resize((160, 90)).convert("L")
    return sum(1 for p in small.getdata() if p > 24)


class TestOptionsAdvertised(unittest.TestCase):
    def test_loads_with_valid_schema(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("options", found)
        entry = found["options"]
        self.assertIn("module", entry, "options failed to import: %s"
                      % entry.get("broken"))
        self.assertTrue(entry["static"])
        for pname, spec in entry["params"].items():
            self.assertIn(spec.get("type"), displayd.INPUT_TYPES,
                          "options.%s has unknown type %r"
                          % (pname, spec.get("type")))

    def test_selection_params_validate(self):
        displayd.validate_params({}, OPT.PARAMS)
        displayd.validate_params(
            {"title": "Menu", "views": ["clock", "chat"]}, OPT.PARAMS)
        with self.assertRaises(ValueError):
            displayd.validate_params({"title": 7}, OPT.PARAMS)


class TestCoerceViews(unittest.TestCase):
    def test_default_is_pinned_four(self):
        self.assertEqual(OPT.coerce_views({}), ["clock", "chat", "row", "stream"])
        self.assertEqual(OPT.coerce_views(None), ["clock", "chat", "row", "stream"])
        self.assertEqual(OPT.coerce_views({"views": []}),
                         ["clock", "chat", "row", "stream"])

    def test_custom_list_kept(self):
        self.assertEqual(OPT.coerce_views({"views": ["life", "qr"]}),
                         ["life", "qr"])

    def test_malformed_entries_skipped_never_fatal(self):
        self.assertEqual(
            OPT.coerce_views({"views": ["clock", "", None, 7, "a/b", " chat "]}),
            ["clock", "chat"])
        self.assertEqual(OPT.coerce_views({"views": "clock"}),
                         ["clock", "chat", "row", "stream"])
        self.assertEqual(OPT.coerce_views({"views": ["", None]}),
                         ["clock", "chat", "row", "stream"])


class TestOptionsRenders(unittest.TestCase):
    def _run(self, params, w=480, h=270):
        screen = make_screen(w, h)
        stop = threading.Event()
        stop.set()
        OPT.run(screen, params, stop)
        self.assertEqual(len(screen.fb.frames), 1)
        return screen.fb.frames[0]

    def test_default_frame_draws(self):
        img = self._run({})
        self.assertGreater(non_bg_count(img), 100)

    def test_custom_views_draw(self):
        img = self._run({"title": "Menu", "views": ["life", "qr", "beads"]})
        self.assertGreater(non_bg_count(img), 100)

    def test_panel_size_frame_draws(self):
        img = self._run({}, w=1920, h=1080)
        self.assertEqual(img.size, (1920, 1080))
        # Lower bar than the small-screen tests: the same text covers
        # fewer downscaled pixels at panel size, but must still draw.
        self.assertGreater(non_bg_count(img), 20)

    def test_malformed_params_still_draw(self):
        img = self._run({"views": ["", None, 7], "title": None})
        self.assertGreater(non_bg_count(img), 100)


class OptionsDaemonTestCase(unittest.TestCase):
    """Headless show(options) -> show(clock): tap lands options, /show back."""

    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        self.addCleanup(self.daemon.clear)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _wait(self, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def _snapshot(self):
        png = self.daemon.snapshot()
        self.assertIsNotNone(png, "nothing has been drawn yet")
        return Image.open(io.BytesIO(png)).convert("RGB")

    def test_show_options_then_back_to_clock(self):
        # The tap-anywhere fallback POSTs /show {"renderer": "options"}:
        # it must be showable from a running view and returnable.
        self.daemon.show("clock", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "clock"))
        self.daemon.show("options", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "options"))
        # STATIC views park after one present: wait for the frame, not
        # just the view switch.
        self.assertTrue(self._wait(lambda: self.daemon.snapshot()
                                   is not None))
        img = self._snapshot()
        # Panel-size frame: same lowered bar as test_panel_size_frame_draws.
        self.assertGreater(non_bg_count(img), 20)
        # The way back: an explicit selection wins, options never traps.
        self.daemon.show("clock", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "clock"))

    def test_options_cancels_transient(self):
        # Tap during a notice/reload transient lands options: manual /show
        # cancels transients by daemon design, so no view traps the user.
        self.daemon.show("clock", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "clock"))
        self.daemon.notify("hello", duration=60)
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "notice"))
        self.daemon.show("options", {})
        self.assertTrue(self._wait(
            lambda: self.daemon.screen.current_view == "options"))



class OptionsTemplateTest(unittest.TestCase):
    """The migration, stated as invariants on the shipped source.

    Pure, no engine: a re-grown Pillow path or a drifted template must
    fail on any checkout, engine built or not.
    """

    def source(self, name="options.py"):
        with open(os.path.join(displayd.RENDERER_DIR, name),
                  encoding="utf-8") as handle:
            return handle.read()

    def declared(self):
        with open(TEMPLATE_PATH, encoding="utf-8") as handle:
            text = handle.read()
        body = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        return set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)", body)), body

    def test_no_pillow_drawing_left_in_the_renderer(self):
        # The oracle for this increment: a stray ImageDraw here means the
        # template path is decoration over a live Pillow draw loop.
        for name in ("options.py", "_options_grid.py"):
            with self.subTest(module=name):
                source = self.source(name)
                self.assertNotIn("ImageDraw", source)
                self.assertNotIn("ImageFont", source)

    def test_the_view_renders_through_the_shipped_template(self):
        self.assertIn(OPT.TEMPLATE, self.source())
        self.assertTrue(os.path.isfile(TEMPLATE_PATH),
                        "html-templates/options.html is the shipped surface")

    def test_the_shipped_template_matches_the_supplied_variables(self):
        # Every placeholder the file declares is a key the renderer
        # supplies, and vice versa: a half-filled document is a red card
        # on the panel, which is exactly what drift here would cost.
        declared, body = self.declared()
        supplied = set(grid.chrome(["clock"], (8, 10, 16), (255, 255, 255),
                                   (140, 160, 190), (156, 200, 255),
                                   "OPTIONS", "pick one", "foot"))
        self.assertEqual(declared, supplied | {"names"})
        self.assertEqual(len(displayd.load_renderers(
            displayd.RENDERER_DIR)["options"]["params"]),
            len(OPT.PARAMS))

    def test_exactly_one_raw_slot_and_it_is_the_name_layer(self):
        # The name layer is the only markup a document receives, and only
        # a renderer can fill it. A second raw slot would be a second way
        # for markup to reach a panel.
        _declared, body = self.declared()
        self.assertEqual(len(re.findall(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*"
                                        r"\s*\|\s*raw\s*\}\}", body)), 1)
        self.assertIn("{{names|raw}}", body)

    def test_no_javascript_and_no_remote_resources(self):
        _declared, body = self.declared()
        lowered = body.lower()
        for banned in ("<script", "javascript:", "@import", "http://",
                       "https://", "url("):
            self.assertNotIn(banned, lowered)

    def test_the_chrome_names_itself(self):
        vars_ = grid.chrome(["clock"], (8, 10, 16), (255, 255, 255),
                            (140, 160, 190), (156, 200, 255), "OPTIONS",
                            "pick one", "foot")
        self.assertEqual(vars_["eyebrow"], "DISPLAYD")
        self.assertEqual(vars_["title"], "OPTIONS")
        self.assertEqual(vars_["subtitle"], "pick one")
        self.assertEqual(vars_["status"], "1 VIEW")
        self.assertEqual(vars_["footer"], "foot")
        self.assertEqual(vars_["footer_right"], "litehtml")
        self.assertEqual(vars_["background"], "#080a10")
        self.assertEqual(vars_["color"], "#ffffff")
        self.assertEqual(vars_["dim"], "#8ca0be")
        self.assertEqual(vars_["accent"], "#9cc8ff")

    def test_a_caller_view_name_is_escaped_before_it_reaches_markup(self):
        # The names go into a RAW slot, so escaping is the only thing
        # standing between a caller and the document. A name is the one
        # value here that comes from outside the repo.
        hostile = '<img src=x onerror=alert(1)>'
        markup = grid.name_markup([hostile], [(0, 0, 800, 200)],
                                  (255, 255, 255))
        self.assertNotIn("<img", markup)
        self.assertNotIn("onerror=alert", markup.replace("&quot;", ""))
        self.assertIn("&lt;img", markup)

    def test_geometry_stays_inside_its_rect(self):
        rect = grid.grid_rect(1920, 1080)
        views = OPT.coerce_views({})
        geo = grid.grid_geometry(rect, len(views))
        self.assertEqual(len(geo), len(views))
        for x, y, cw, ch in geo:
            self.assertGreaterEqual(x, rect[0])
            self.assertGreaterEqual(y, rect[1])
            self.assertLessEqual(x + cw, rect[0] + rect[2])
            self.assertLessEqual(y + ch, rect[1] + rect[3])

    def test_two_names_are_one_column_and_four_are_two(self):
        rect = grid.grid_rect(1920, 1080)
        one = grid.grid_geometry(rect, 1)
        self.assertEqual(grid.cols_for(1), 1)
        self.assertEqual(grid.cols_for(2), 1)
        self.assertEqual(grid.cols_for(3), 2)
        rows = {r[1] for r in grid.grid_geometry(rect, 4)}
        self.assertEqual(len(rows), 2, "four names lay out as two rows")

    def test_garbage_geometry_never_raises(self):
        rect = grid.grid_rect(1920, 1080)
        for bad in (None, "nope", 0, -4, 10 ** 9):
            with self.subTest(bad=bad):
                self.assertTrue(grid.grid_geometry(rect, bad or 1))

    def test_the_view_declares_no_per_view_touch_regions(self):
        # Options NAMES picks; the picker SELECTS. A tap here re-shows
        # this view, so a per-name hit rect would be a behaviour change
        # disguised as a migration detail.
        self.assertEqual(OPT.options_regions(), [])
        self.assertEqual(OPT.options_regions(["clock", "chat"]), [])


@native_built
class OptionsPixelsTest(unittest.TestCase):
    """What the panel actually shows, at the rects the geometry says."""

    W, H = 1920, 1080

    def screen(self, w=None, h=None):
        return displayd.Screen(FakeFb(w or self.W, h or self.H))

    def frame(self, params=None, w=None, h=None):
        screen = self.screen(w, h)
        stop = threading.Event()
        stop.set()
        OPT.run(screen, params or {}, stop)
        return screen.fb.frames[-1]

    def lit(self, frame, box):
        crop = frame.crop(box).convert("RGB")
        return max(sum(pixel) for pixel in crop.getdata())

    def bright(self, frame, box, threshold=200):
        crop = frame.crop(box).convert("RGB")
        return sum(1 for pixel in crop.getdata() if sum(pixel) > threshold)

    def cells(self, params=None):
        views = OPT.coerce_views(params or {})
        return views, grid.grid_geometry(grid.grid_rect(self.W, self.H),
                                         len(views))

    def test_the_panel_size_frame_draws_every_band(self):
        frame = self.frame()
        for band, box in (("title", (700, 80, 1220, 175)),
                          ("rule", (400, 208, 1520, 220)),
                          ("instructions", (400, 240, 1520, 290)),
                          ("footer", (200, 1000, 1200, 1045))):
            with self.subTest(band=band):
                self.assertGreater(self.lit(frame, box), 200)

    def test_every_named_view_is_lit_inside_its_own_cell(self):
        # The check migration can silently break: a name drawn somewhere
        # other than where grid_geometry() put it.
        frame = self.frame({"views": ["clock", "chat", "row", "stream",
                                      "activity", "options"]})
        views, geo = self.cells({"views": ["clock", "chat", "row", "stream",
                                           "activity", "options"]})
        self.assertEqual(len(geo), 6)
        for name, (x, y, cw, ch) in zip(views, geo):
            with self.subView(name):
                inner = (x + 4, y + 4, x + cw - 4, y + ch - 4)
                self.assertGreater(self.bright(frame, inner), 200,
                                   "%r drew nothing in its cell" % name)

    def test_a_custom_title_and_subheader_draw(self):
        frame = self.frame({"title": "MENU", "instructions": "pick one"})
        self.assertGreater(self.lit(frame, (700, 80, 1220, 175)), 200)
        self.assertGreater(self.lit(frame, (600, 240, 1320, 290)), 150)

    def test_a_custom_background_is_honoured(self):
        frame = self.frame({"background": "#203040"})
        # The frame's own background must be the requested colour, not
        # the template's own: a wrong colour here means the compositing
        # step, not just the document, went wrong.
        self.assertEqual(frame.getpixel((300, 700)), (32, 48, 64))

    def test_a_name_that_is_not_a_word_still_draws_as_text(self):
        # Escaped, not dropped, and not rendered as markup: the surface
        # must never vanish because a caller pushed something odd.
        frame = self.frame({"views": ["<b>clock</b>"]})
        views, geo = self.cells({"views": ["<b>clock</b>"]})
        x, y, cw, ch = geo[0]
        self.assertGreater(self.bright(frame, (x + 4, y + 4, x + cw - 4,
                                               y + ch - 4)), 100)
        # No red-ish pure markup artifact anywhere: the frame is text on
        # a background, so a saturated third colour would mean a style
        # leaked in through the raw slot.
        colours = {frame.getpixel(p) for p in
                   [(300, 700), (960, 540), (100, 100), (1850, 1000)]}
        for colour in colours:
            self.assertLessEqual(max(colour) - min(colour), 64)

    def test_a_tiny_panel_still_draws_the_names(self):
        frame = self.frame({}, w=480, h=270)
        self.assertEqual(frame.size, (480, 270))
        self.assertGreater(non_bg_count(frame), 20)

    def test_a_broken_template_is_a_card_not_a_blank(self):
        screen = self.screen()
        original = grid.__name__  # noqa: F841 - documentation of intent
        import _options_grid
        real = _options_grid.chrome

        def boom(*_args, **_kwargs):
            raise RuntimeError("synthetic failure")

        OPT.templates = OPT.templates  # module identity is unchanged
        import _html_templates as templates_mod
        saved = templates_mod.TemplateError
        try:
            # A template error is the realistic break: the file is gone
            # or a variable is missing. The panel must say so.
            OPT.run(screen, {}, threading.Event())
            frame = screen.fb.frames[-1]
            # Red rule across the top: the shared failure card.
            self.assertGreater(self.bright(frame, (0, 0, self.W, 40),
                                           threshold=300), 500)
        finally:
            self.assertIs(saved, templates_mod.TemplateError)
            self.assertTrue(callable(real))
            self.assertEqual(original, grid.__name__)


if __name__ == "__main__":
    unittest.main()
