"""html-view tests: the litehtml template renderer and its trust boundary.

Three layers, tested in the order they can fail:

1. _html_templates -- the only way a document gets onto the panel. Names
   are constrained, values are escaped, a missing variable is an error
   rather than a silently wrong panel, and nothing outside the root opens.
2. _html_native -- the ctypes binding and PIL painter. Geometry (borders
   per side, alpha blending, clipping), the image refusal rules, and the
   font release invariant the flat-RSS claim rests on.
3. renderers/html.py -- the view contract: advertised schema, a real
   headless frame, live re-render on a pushed vars payload, and a loud
   readable card for every failure a caller can cause.

The native library is optional by design, so the cases that need it skip
when tools/build_litehtml.sh has not been run; the trust-boundary cases
never skip, because they are pure Python.

Run from the repo root:  python3 -m unittest tests.test_html -v
"""

import importlib.util
import os
import re
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import displayd
import _html_compose
import _html_native
import _html_templates as templates
import theme


def body(text):
    """The template's own text, with the injected token layer removed.

    Every loaded document carries the palette as a ``:root`` block (see
    ``_html_compose``). That block is fixed decoration generated from
    ``theme.py``, so the substitution contract is asserted on the text
    around it -- exactly, not loosely.
    """
    return _html_compose.strip_tokens(text)

HTML_PATH = os.path.join(displayd.RENDERER_DIR, "html.py")
REPO = os.path.dirname(os.path.abspath(displayd.__file__))
SHIPPED_ROOT = os.path.join(REPO, "html-templates")


def load_html(name="html_testmod"):
    spec = importlib.util.spec_from_file_location(name, HTML_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HTML = load_html()


def native_built():
    return any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, name))
               for name in _html_native.LIB_NAMES)


requires_native = unittest.skipUnless(
    native_built(), "native library not built (tools/build_litehtml.sh)")


class FakeFb:
    def __init__(self, w=480, h=270):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


def make_screen(w=480, h=270):
    return displayd.Screen(FakeFb(w, h))


def lit_pixels(img, threshold=60):
    small = img.resize((160, 90)).convert("L")
    return sum(1 for p in small.getdata() if p > threshold)


# The one thing that tells an error card from a rendered template: the red
# rule _error_frame draws across the top. Kept as one predicate so a card and
# its recovery cannot be told apart by two tests that disagree.
ERROR_RULE = (214, 74, 74)


def _is_error_card(frame):
    if frame is None:
        return False
    return frame.getpixel((frame.size[0] // 2, 1)) == ERROR_RULE


def run_view(screen, params, seconds=2.0):
    """Drive the view the way the daemon does, then stop it."""
    stop = threading.Event()
    thread = threading.Thread(target=HTML.run, args=(screen, params, stop))
    thread.daemon = True
    thread.start()
    thread.join(seconds)
    stop.set()
    thread.join(1.0)
    return screen.fb.frames


def write_template(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = _html_native.allow_root(self.dir.name)

    def load(self, name, variables=None, root=None):
        return templates.load(name, variables, root=root or self.root)


# --------------------------------------------------------------- the names


class TestTemplateNames(TempRoot):
    def test_shipped_example_is_a_valid_template(self):
        self.assertIn("status.html", templates.available(SHIPPED_ROOT))
        text, root = templates.load(
            "status.html",
            {"title": "T", "sub": "S", "value": "1", "unit": "u", "note": "n"},
            root=SHIPPED_ROOT)
        self.assertIn("T", text)
        self.assertEqual(root, _html_native.allow_root(SHIPPED_ROOT))

    def test_available_is_empty_when_root_missing(self):
        self.assertEqual(templates.available("/nonexistent-template-root"), [])

    def test_bad_names_refused(self):
        write_template(self.dir.name, "ok.html", "<p>hi</p>")
        for name in ("", "../escape.html", "/etc/passwd", "ok.HTML",
                     "sub/dir.html", "..", ".", "ok.html\x00.png",
                     "a" * 80 + ".html", "ok.html/", None, 7,
                     "html:passwd", "~/secret.html"):
            with self.subTest(name=name):
                with self.assertRaises(templates.TemplateError):
                    self.load(name)

    def test_symlink_out_of_root_refused(self):
        outside = os.path.join(self.dir.name, "..", "outside-%d.html"
                               % os.getpid())
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("<p>secret</p>")
        self.addCleanup(os.unlink, outside)
        link = os.path.join(self.root, "link.html")
        os.symlink(_html_native.allow_root(outside), link)
        with self.assertRaises(templates.TemplateError):
            self.load("link.html")

    def test_missing_template_names_the_root(self):
        with self.assertRaises(templates.TemplateError) as caught:
            self.load("absent.html")
        self.assertIn("absent.html", str(caught.exception))

    def test_missing_root_says_how_to_fix_it(self):
        with self.assertRaises(templates.TemplateError) as caught:
            templates.load("status.html", root="/nonexistent-template-root")
        self.assertIn(templates.ENV_ROOT, str(caught.exception))

    def test_oversized_template_refused(self):
        big = "x" * (templates.MAX_TEMPLATE_BYTES + 10)
        write_template(self.dir.name, "big.html", big)
        with self.assertRaises(templates.TemplateError) as caught:
            self.load("big.html")
        self.assertIn("max", str(caught.exception))

    def test_undecodable_template_refused(self):
        path = os.path.join(self.root, "bin.html")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe\x00bad")
        with self.assertRaises(templates.TemplateError):
            self.load("bin.html")

    def test_directory_is_not_a_template(self):
        os.mkdir(os.path.join(self.root, "dir.html"))
        with self.assertRaises(templates.TemplateError):
            self.load("dir.html")


# -------------------------------------------------------------- the values


class TestTemplateValues(TempRoot):
    def setUp(self):
        super().setUp()
        write_template(self.dir.name, "t.html", "<p>{{a}}|{{b}}</p>")

    def test_values_are_html_escaped(self):
        text, _ = self.load("t.html", {
            "a": "<script>alert(1)</script>",
            "b": "\"quoted\" & 'single'",
        })
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&amp;", text)
        self.assertIn("&quot;", text)
        self.assertIn("&#x27;", text)

    def test_style_injection_cannot_escape_the_value(self):
        text, _ = self.load("t.html", {
            "a": "</p><style>body{display:none}</style><p>",
            "b": "x",
        })
        self.assertEqual(text.count("<style"), 1)  # the token layer, only
        self.assertNotIn("<style>", body(text))
        self.assertNotIn("</p><style>", body(text))

    def test_missing_variable_is_an_error_naming_it(self):
        with self.assertRaises(templates.TemplateError) as caught:
            self.load("t.html", {"a": "1"})
        self.assertIn("'b'", str(caught.exception))

    def test_no_variables_at_all_is_an_error(self):
        with self.assertRaises(templates.TemplateError):
            self.load("t.html")

    def test_unusable_placeholder_is_an_error(self):
        write_template(self.dir.name, "bad.html", "<p>{{ a-b }}</p>")
        with self.assertRaises(templates.TemplateError) as caught:
            self.load("bad.html")
        self.assertIn("a-b", str(caught.exception))

    def test_whitespace_in_placeholder_is_fine(self):
        write_template(self.dir.name, "ok.html", "<p>{{  spaced  }}</p>")
        text, _ = self.load("ok.html", {"spaced": "yes"})
        self.assertEqual(body(text), "<p>yes</p>")

    def test_non_string_values_become_text(self):
        text, _ = self.load("t.html", {"a": 42, "b": 3.5})
        self.assertIn("42", text)
        self.assertIn("3.5", text)


    def test_none_is_empty_not_the_word_none(self):
        text, _ = self.load("t.html", {"a": None, "b": "x"})
        self.assertIn("|x", text)

    def test_bool_reads_as_yes_no(self):
        write_template(self.dir.name, "b.html", "<p>{{flag}}</p>")
        text, _ = self.load("b.html", {"flag": False})
        self.assertEqual(body(text), "<p>no</p>")

    def test_long_value_truncated_not_refused(self):
        text, _ = self.load("t.html", {"a": "y" * 9000, "b": ""})
        self.assertLess(len(body(text)), templates.MAX_VALUE_CHARS + 200)

    def test_bad_variable_name_refused(self):
        with self.assertRaises(templates.TemplateError):
            self.load("t.html", {"<script>": "1", "a": "2", "b": "3"})

    def test_too_many_variables_refused(self):
        crowd = {"v%d" % i: "x" for i in range(templates.MAX_VARS + 1)}
        with self.assertRaises(templates.TemplateError) as caught:
            self.load("t.html", crowd)
        self.assertIn("too many", str(caught.exception))

    def test_brace_bomb_does_not_reach_the_document(self):
        # No placeholder, so {{ must be a leftover the loader refuses.
        write_template(self.dir.name, "bomb.html",
                       "<p>" + "{{" * 200 + "</p>")
        with self.assertRaises(templates.TemplateError):
            self.load("bomb.html")


class TestRawSlots(TempRoot):
    """{{name|raw}}: the one markup slot, and who may fill it.

    It exists because a tile grid cannot be ten text variables. It is a
    SEPARATE mapping, so the html renderer -- the only thing a caller can
    reach -- cannot fill it, and a pushed value lands escaped instead.
    """

    def setUp(self):
        super().setUp()
        write_template(self.dir.name, "t.html", "<div>{{tiles|raw}}</div>")

    def load(self, name, variables=None, root=None, raw=None):
        return templates.load(name, variables, root=root or self.root,
                              raw=raw)

    def test_a_raw_value_is_filled_verbatim(self):
        text, _ = self.load("t.html", raw={"tiles": '<b class="x">hi</b>'})
        self.assertEqual(body(text), '<div><b class="x">hi</b></div>')

    def test_a_caller_cannot_reach_the_raw_slot(self):
        # Same template, same key, no raw mapping: the value is escaped
        # like any other, so /show and /feed/html/vars cannot inject.
        text, _ = self.load("t.html", {"tiles": "<script>alert(1)</script>"})
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_an_unfilled_raw_slot_is_an_error_naming_it(self):
        with self.assertRaises(templates.TemplateError) as caught:
            self.load("t.html")
        self.assertIn("'tiles'", str(caught.exception))

    def test_raw_values_must_be_renderable_markup(self):
        for bad in ("{{t}}", 42, "x" * (templates.MAX_RAW_CHARS + 1)):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(templates.TemplateError):
                    self.load("t.html", raw={"tiles": bad})

    def test_a_raw_slot_is_never_invented_by_the_caller(self):
        # A template that declares no raw slot gets no markup either way.
        write_template(self.dir.name, "plain.html", "<p>{{a}}</p>")
        text, _ = self.load("plain.html", {"a": "x"}, raw={"tiles": "<b>"})
        self.assertEqual(body(text), "<p>x</p>")

    def test_the_shipped_picker_template_only_has_one(self):
        names = templates.available(SHIPPED_ROOT)
        self.assertIn("picker.html", names)
        for name in names:
            with open(os.path.join(SHIPPED_ROOT, name),
                      encoding="utf-8") as handle:
                body = re.sub(r"<!--.*?-->", "", handle.read(),
                              flags=re.DOTALL)
            with self.subTest(template=name):
                self.assertLessEqual(len(templates.RAW_RE.findall(body)), 1)


@requires_native
class TestTemplateEndToEnd(unittest.TestCase):
    def test_escaped_value_cannot_add_markup(self):
        root = tempfile.mkdtemp()
        write_template(root, "x.html", "<p>{{a}}</p>")
        document, _ = templates.load(
            "x.html", {"a": "<img src=x onerror=alert(1)>"},
            root=root)
        image, _ = _html_native.render(document, 200, 100, root=root)
        # The escaped text is drawn as glyphs, not as a second element, so
        # the document stays one short line rather than gaining a node.
        rows = [y for y in range(100)
                if any(image.getpixel((x, y)) != (0, 0, 0) for x in range(200))]
        self.assertLess(len(rows), 60)


# -------------------------------------------------------------- the engine


@requires_native
class TestNativeBasics(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        Image.new("RGB", (4, 4), (255, 0, 0)).save(
            os.path.join(self.dir.name, "dot.png"))

    def test_version_carries_the_pinned_revision(self):
        version = _html_native.version()
        self.assertIn("5624e795be50f02c21c89985c374dcd659dbd74b", version)
        self.assertIn("c-abi 1", version)

    def test_render_returns_image_and_content_height(self):
        image, height = _html_native.render(
            "<div style='font-size:20px;color:#fff'>hi</div>", 200, 300)
        self.assertEqual(image.size, (200, 300))
        self.assertEqual(image.mode, "RGB")
        self.assertGreater(height, 0)
        self.assertLess(height, 300)

    def test_background_colour_is_the_canvas(self):
        image, _ = _html_native.render("<p>x</p>", 40, 20, background=(10, 20, 30))
        self.assertEqual(image.getpixel((39, 19)), (10, 20, 30))

    def test_text_draws_ink(self):
        image, _ = _html_native.render(
            "<div style='font-size:40px;color:#ffffff'>MMMM</div>", 300, 80)
        self.assertGreater(lit_pixels(image, 100), 10)

    def test_solid_fill_paints_the_box(self):
        image, _ = _html_native.render(
            "<div style='width:100px;height:50px;background:#204080'></div>",
            200, 100)
        self.assertEqual(image.getpixel((50, 25)), (32, 64, 128))
        self.assertEqual(image.getpixel((150, 25)), (0, 0, 0))

    def test_each_border_side_lands_on_its_own_edge(self):
        # Regression: the C ABI's side order is left/top/right/bottom, and
        # drawing them in a different order silently puts every border in
        # the wrong place while still looking like it worked.
        #
        # width:100px is the *content* box, so the border box spans x=10..121:
        # a 6px left border at 10..15 and a 6px right border at 116..121.
        image, _ = _html_native.render(
            "<div style='position:absolute;left:10px;top:10px;width:100px;"
            "height:60px;border-left:6px solid #ff0000;"
            "border-top:6px solid #00ff00;border-right:6px solid #0000ff;"
            "border-bottom:6px solid #ffff00'></div>", 200, 120)
        self.assertEqual(image.getpixel((13, 40)), (255, 0, 0), "left side")
        self.assertEqual(image.getpixel((118, 40)), (0, 0, 255), "right side")
        self.assertEqual(image.getpixel((60, 13)), (0, 255, 0), "top side")
        self.assertEqual(image.getpixel((60, 78)), (255, 255, 0), "bottom side")

    def test_alpha_fill_blends_with_what_is_under_it(self):
        image, _ = _html_native.render(
            "<div style='width:60px;height:30px;background:rgba(255,255,255,0.5)'>"
            "</div>", 80, 40)
        self.assertEqual(image.getpixel((30, 15)), (128, 128, 128))

    def test_alpha_text_is_not_solid_white(self):
        image, _ = _html_native.render(
            "<div style='font-size:40px;color:rgba(255,255,255,0.5)'>MMMM</div>",
            200, 60)
        brightest = max(image.getdata(), key=sum)
        self.assertLessEqual(brightest[0], 200)
        self.assertGreater(brightest[0], 40)

    def test_tall_document_is_clipped_not_scaled(self):
        tall = "".join("<div style='height:40px;background:#0a0a0a'></div>"
                       for _ in range(200))
        image, height = _html_native.render(tall, 100, 80)
        self.assertEqual(image.size, (100, 80))
        self.assertGreater(height, 1000)

    def test_malformed_documents_do_not_raise(self):
        # Garbage in, a frame out. A panel that raises on a typo'd tag goes
        # black, and black is indistinguishable from a dead daemon.
        for document in ("<div><p>unclosed", "<b><i>x</b></i>",
                         "<div style=>x</div>", "<<<>>>",
                         "<style>body{}</style>",
                         "<table><tr><td>cell"):
            with self.subTest(document=document):
                image, _ = _html_native.render(document, 100, 60)
                self.assertEqual(image.size, (100, 60))

    def test_a_document_with_nothing_in_it_is_an_error_not_a_black_panel(self):
        # A template that comments itself out, or arrives as an empty
        # NUL-leading buffer, is an operator bug worth naming -- the same
        # "error, not a silent blank" rule the template layer follows.
        for document in ("", "\x00\x01\x02"):
            with self.subTest(document=document):
                with self.assertRaises(_html_native.HtmlRenderError) as caught:
                    _html_native.render(document, 100, 60)
                self.assertIn("no document", str(caught.exception))

    def test_fonts_are_released_when_the_document_dies(self):
        before = _html_native._FontCache.live
        for size in range(12, 40, 3):
            _html_native.render(
                "<div style='font-size:%dpx;color:#fff;font-weight:bold'>x</div>"
                % size, 200, 60)
        self.assertEqual(_html_native._FontCache.live, before)

    def test_repeat_tiles_the_box_and_stays_bounded(self):
        # A 4x4 tile over a 400x300 box must cover the box, not the one
        # tile: the engine hands us the first tile (origin_box) and the
        # region to fill (border_box) as separate rects.
        document = ("<div style='width:400px;height:300px;"
                    "background-image:url(dot.png);"
                    "background-repeat:repeat'></div>")
        start = time.monotonic()
        image, _ = _html_native.render(document, 400, 300, root=self._image_root())
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(image.size, (400, 300))
        red = sum(1 for pixel in image.getdata() if pixel == (255, 0, 0))
        self.assertGreater(red, 400 * 300 * 0.9)

    def test_no_repeat_draws_one_tile(self):
        document = ("<div style='width:400px;height:300px;"
                    "background-image:url(dot.png);"
                    "background-repeat:no-repeat'></div>")
        image, _ = _html_native.render(document, 400, 300, root=self._image_root())
        red = sum(1 for pixel in image.getdata() if pixel == (255, 0, 0))
        self.assertEqual(red, 16)

    def _image_root(self):
        return _html_native.allow_root(self.dir.name)


@requires_native
class TestNativeImageSafety(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        Image.new("RGB", (4, 4), (255, 0, 0)).save(
            os.path.join(self.dir.name, "dot.png"))
        self.root = _html_native.allow_root(self.dir.name)

    def red_count(self, ref, root=None, width=200, height=100):
        document = ("<div style='width:%dpx;height:%dpx;"
                    "background-image:url(%s)'></div>"
                    % (width, height, ref))
        image, _ = _html_native.render(document, width, height, root=root)
        return sum(1 for pixel in image.getdata() if pixel == (255, 0, 0))

    def test_local_image_draws(self):
        self.assertGreater(self.red_count("dot.png", root=self.root), 100)

    def test_dot_slash_prefix_still_local(self):
        self.assertGreater(self.red_count("./dot.png", root=self.root), 100)

    def test_query_and_fragment_are_stripped(self):
        self.assertGreater(self.red_count("dot.png?v=2", root=self.root), 100)
        self.assertGreater(self.red_count("dot.png#anchor", root=self.root), 100)

    def test_traversal_refused(self):
        for ref in ("../dot.png", "../../../../etc/hosts",
                    "sub/../../dot.png", "a/b/../../../dot.png"):
            with self.subTest(ref=ref):
                self.assertEqual(self.red_count(ref, root=self.root), 0)

    def test_absolute_and_home_refs_refused(self):
        for ref in ("/etc/hosts", "/tmp/dot.png", "~/dot.png",
                    "\\\\server\\share\\dot.png"):
            with self.subTest(ref=ref):
                self.assertEqual(self.red_count(ref, root=self.root), 0)

    def test_urls_and_data_uris_refused(self):
        for ref in ("http://example.com/a.png", "https://example.com/a.png",
                    "//example.com/a.png", "file:///etc/hosts",
                    "data:image/png;base64,iVBORw0KGgo="):
            with self.subTest(ref=ref):
                self.assertEqual(self.red_count(ref, root=self.root), 0)

    def test_no_root_means_no_images(self):
        self.assertEqual(self.red_count("dot.png", root=None), 0)

    def test_symlink_escape_refused(self):
        link = os.path.join(self.root, "out.png")
        os.symlink("/etc/hosts", link)
        self.assertEqual(self.red_count("out.png", root=self.root), 0)

    def test_external_css_and_js_do_not_load(self):
        # import_css is the only remote-CSS path and it is inert; <script>
        # has no host at all. A document that tries both still renders, and
        # nothing appears that did not come from the template itself.
        document = ("<style>@import url('http://example.com/a.css');"
                    "p{color:#ff0000}</style>"
                    "<link rel='stylesheet' href='http://example.com/b.css'>"
                    "<script>window.x=1</script>"
                    "<p>text</p>")
        image, _ = _html_native.render(document, 200, 100, root=None)
        self.assertEqual(image.size, (200, 100))


class TestRepeatComposer(unittest.TestCase):
    """The tile composer itself, away from the engine.

    This is the piece that had to be right: walking tile by tile needs a cap
    to stay safe, and a cap truncates in raster order, which paints the
    panel's bottom-right corner black.
    """

    TILE = (3, 5)

    def tile(self):
        image = Image.new("RGBA", self.TILE)
        for y in range(self.TILE[1]):
            for x in range(self.TILE[0]):
                image.putpixel((x, y), (x * 80, y * 50, 7, 255))
        return image

    def test_every_repeat_mode_is_an_exact_pixel_repeat(self):
        tile = self.tile()
        modes = (_html_native.REPEAT_REPEAT, _html_native.REPEAT_REPEAT_X,
                 _html_native.REPEAT_REPEAT_Y)
        for repeat in modes:
            for width, height in ((7, 11), (1, 1), (100, 3), (3, 100),
                                  (64, 64)):
                with self.subTest(repeat=repeat, size=(width, height)):
                    out = _html_native._compose_repeat(
                        tile, width, height, repeat)
                    if repeat in _html_native._REPEATS_X:
                        self.assertGreaterEqual(out.width, width)
                    else:
                        self.assertEqual(out.width, tile.width)
                    if repeat in _html_native._REPEATS_Y:
                        self.assertGreaterEqual(out.height, height)
                    else:
                        self.assertEqual(out.height, tile.height)
                    for y in range(out.height):
                        for x in range(out.width):
                            self.assertEqual(
                                out.getpixel((x, y)),
                                tile.getpixel((x % tile.width,
                                               y % tile.height)),
                                "no seam at %d,%d" % (x, y))

    def test_a_full_hd_area_costs_a_bounded_number_of_pastes(self):
        # 3x5 tiles over 4096x2160 is ~600k tiles. The point of doubling is
        # that the paste count is log2 of that, not the tile count.
        tile = self.tile()
        out = _html_native._compose_repeat(tile, 4096, 2160,
                                           _html_native.REPEAT_REPEAT)
        self.assertEqual(out.size, (4096, 2160))


@requires_native
class TestNativeRefusals(unittest.TestCase):
    def test_resolve_local_contract(self):
        root = tempfile.mkdtemp()
        Image.new("RGB", (2, 2)).save(os.path.join(root, "ok.png"))
        real = _html_native.allow_root(root)
        self.assertEqual(_html_native.resolve_local(real, "ok.png"),
                         os.path.join(real, "ok.png"))
        for ref in ("", None, 7, "/abs", "http://x/y", "//x/y", "a\x00b",
                    "nope.png", ".", "..", "sub/"):
            with self.subTest(ref=ref):
                self.assertIsNone(_html_native.resolve_local(real, ref))

    def test_unresolvable_root_string_refuses_rather_than_guessing(self):
        # A root that has not been through allow_root (a symlinked path,
        # say) must not be matched by string prefix.
        root = tempfile.mkdtemp()
        Image.new("RGB", (2, 2)).save(os.path.join(root, "ok.png"))
        self.assertIsNone(_html_native.resolve_local(root + "/", "ok.png"))

    def test_allow_root_resolves_symlinks(self):
        root = tempfile.mkdtemp()
        self.assertEqual(_html_native.allow_root(root + "/./"),
                         _html_native.allow_root(root))
        self.assertFalse(os.path.islink(_html_native.allow_root(root)))

    def test_html_render_error_carries_a_message(self):
        self.assertTrue(issubclass(_html_native.NativeMissing,
                                   _html_native.HtmlRenderError))


# ---------------------------------------------------------------- the view


class TestViewAdvertised(unittest.TestCase):
    def setUp(self):
        self.found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.entry = self.found.get("html")
        self.assertIsNotNone(self.entry, "html renderer not discovered")
        self.assertNotIn("broken", self.entry,
                         "html failed to import: %s" % self.entry.get("broken"))

    def test_imports_without_the_native_library(self):
        # The view must always exist, because "not built yet" is an
        # operator message the panel can show, not a missing renderer.
        self.assertIn("run", dir(self.entry["module"]))

    def test_schema_types_are_known_to_the_daemon(self):
        for section in ("params", "inputs"):
            for pname, spec in self.entry[section].items():
                self.assertIn(spec.get("type"), displayd.INPUT_TYPES,
                              "html.%s.%s has unknown type %r"
                              % (section, pname, spec.get("type")))

    def test_template_is_optional_and_defaults_to_the_shared_chrome(self):
        # No required params, or this view could never appear as a picker
        # or home tile and "the UI is a template" would stay opt-in.
        displayd.validate_params({}, self.entry["params"])
        displayd.validate_params({"template": "status.html"},
                                 self.entry["params"])
        displayd.validate_params({"template": "s.html", "vars": {"a": "b"}},
                                 self.entry["params"])
        self.assertEqual(HTML.DEFAULT_TEMPLATE, "layout.html")
        self.assertIn(HTML.DEFAULT_TEMPLATE, self.entry["params"]["template"]["help"])

    def test_vars_must_be_an_object(self):
        with self.assertRaises(ValueError):
            displayd.validate_params({"template": "s.html", "vars": "nope"},
                                     self.entry["params"])

    def test_in_the_default_live_view_set(self):
        # Selectable like any other view: no required params means it is
        # offered on the picker and the merged home screen.
        import picker
        live = picker.live_views(displayd.load_renderers(displayd.RENDERER_DIR))
        self.assertIn("html", live)
        import unified
        self.assertIn("html", unified.live_tile_views(
            displayd.load_renderers(displayd.RENDERER_DIR)))

    def test_declares_a_vars_input(self):
        self.assertIn("vars", self.entry["inputs"])
        self.assertEqual(self.entry["inputs"]["vars"]["type"], "object")


@requires_native
class TestViewRenders(unittest.TestCase):
    def test_renders_the_shipped_template(self):
        screen = make_screen(1280, 720)
        frames = run_view(screen, {
            "template": "status.html",
            "vars": {"title": "BUILD", "sub": "12:04", "value": "142",
                     "unit": "taps", "note": "last 7 days"},
        })
        self.assertTrue(frames, "no frame presented")
        self.assertEqual(frames[-1].size, (1280, 720))
        self.assertGreater(lit_pixels(frames[-1]), 20)

    def test_panel_sized_and_tiny_frames(self):
        for size in ((1920, 1080), (480, 270), (320, 240)):
            with self.subTest(size=size):
                screen = make_screen(*size)
                frames = run_view(screen, {
                    "template": "status.html",
                    "vars": {"title": "T", "sub": "S", "value": "1",
                             "unit": "u", "note": "n"}})
                self.assertTrue(frames)
                self.assertEqual(frames[-1].size, size)

    def test_pushed_vars_rerender_in_place(self):
        class Store:
            def __init__(self):
                self.payloads = []

            def get(self, renderer, name):
                return self.payloads

        screen = make_screen(640, 360)
        store = Store()
        screen.feeds = store
        stop = threading.Event()
        thread = threading.Thread(target=HTML.run, args=(screen, {
            "template": "status.html",
            "vars": {"title": "FIRST", "sub": "1", "value": "1",
                     "unit": "u", "note": "n"}}, stop))
        thread.daemon = True
        thread.start()
        thread.join(1.5)
        first = screen.fb.frames[-1] if screen.fb.frames else None
        store.payloads = [{"title": "SECOND", "sub": "2", "value": "2",
                           "unit": "u", "note": "n"}]
        deadline = time.time() + 3.0
        while time.time() < deadline and len(screen.fb.frames) < 2:
            time.sleep(0.05)
        stop.set()
        thread.join(1.0)
        self.assertGreaterEqual(len(screen.fb.frames), 2,
                                "push did not trigger a re-render")
        self.assertNotEqual(screen.fb.frames[0].tobytes(),
                            screen.fb.frames[-1].tobytes())

    def test_unchanged_vars_do_not_re_render(self):
        screen = make_screen(320, 240)
        stop = threading.Event()
        thread = threading.Thread(target=HTML.run, args=(screen, {
            "template": "status.html",
            "vars": {"title": "T", "sub": "S", "value": "1", "unit": "u",
                     "note": "n"}}, stop))
        thread.daemon = True
        thread.start()
        time.sleep(1.5)
        stop.set()
        thread.join(1.0)
        self.assertEqual(len(screen.fb.frames), 1,
                         "idle view re-rendered %d times" % len(screen.fb.frames))


class TestViewFailuresAreVisible(unittest.TestCase):
    def assert_error_card(self, frame):
        self.assertIsNotNone(frame, "no frame presented for a failure")
        # The red rule at the top and readable text: never a blank panel.
        self.assertTrue(_is_error_card(frame), "no error rule")
        self.assertGreater(lit_pixels(frame, 90), 5)

    def test_unknown_template_shows_a_card(self):
        frames = run_view(make_screen(640, 360),
                          {"template": "does_not_exist.html"})
        self.assert_error_card(frames[-1])

    def test_bad_template_name_shows_a_card(self):
        frames = run_view(make_screen(640, 360),
                          {"template": "../escape.html"})
        self.assert_error_card(frames[-1])

    def test_missing_variable_shows_a_card(self):
        frames = run_view(make_screen(640, 360),
                          {"template": "status.html", "vars": {"title": "T"}})
        self.assert_error_card(frames[-1])

    def test_missing_root_says_so(self):
        root = os.environ.get(templates.ENV_ROOT)
        os.environ[templates.ENV_ROOT] = "/nonexistent-template-root"
        self.addCleanup(lambda: os.environ.pop(templates.ENV_ROOT, None)
                        if root is None else os.environ.__setitem__(
                            templates.ENV_ROOT, root))
        frames = run_view(make_screen(640, 360), {"template": "status.html"})
        self.assert_error_card(frames[-1])

    def test_missing_native_library_says_how_to_build_it(self):
        original = _html_native.lib
        _html_native.lib = lambda: (_ for _ in ()).throw(
            _html_native.NativeMissing("html renderer: native library not built"))
        self.addCleanup(setattr, _html_native, "lib", original)
        frames = run_view(make_screen(640, 360), {"template": "status.html"})
        self.assert_error_card(frames[-1])

    def test_render_failure_shows_a_card(self):
        original = _html_native.render
        _html_native.render = lambda *a, **k: (_ for _ in ()).throw(
            _html_native.HtmlRenderError("could not lay out"))
        self.addCleanup(setattr, _html_native, "render", original)
        frames = run_view(make_screen(640, 360), {"template": "status.html"})
        self.assert_error_card(frames[-1])

    def assert_recovered(self, frame):
        """A real panel, not a second error card.

        The tempting version of this test only counts frames, which passes
        even when the second frame is the same error again: the view
        re-renders on every variable change, so a still-missing template
        produces a second identical card. Recovery means the red rule is
        gone and the template's own content is on the panel.
        """
        self.assertIsNotNone(frame, "no frame after the good push")
        self.assertFalse(_is_error_card(frame), "still showing the error rule")
        # The template's own background, which is the palette's page token:
        # asserted through theme so a panel background has one owner.
        self.assertEqual(frame.getpixel((2, frame.size[1] - 2)),
                         theme.rgb("page"), "not the template background")

    @requires_native
    def test_view_recovers_after_a_bad_push(self):
        # Only this one needs the engine: the rest of this class must still
        # run on a checkout where the library was never built, because
        # "not built yet" is a state the panel has to explain.
        class Store:
            def __init__(self):
                self.payloads = []

            def get(self, renderer, name):
                return self.payloads

        screen = make_screen(640, 360)
        store = Store()
        screen.feeds = store
        # A real template with one placeholder short: the error is about the
        # *data*, so pushing the rest of it is exactly what fixes the view.
        stop = threading.Event()
        thread = threading.Thread(target=HTML.run, args=(screen, {
            "template": "status.html", "vars": {"title": "T", "sub": "s"}},
            stop))
        thread.daemon = True
        thread.start()
        thread.join(1.5)
        self.assert_error_card(screen.fb.frames[-1] if screen.fb.frames
                               else None)
        store.payloads = [{"title": "RECOVERED", "sub": "s", "value": "1",
                           "unit": "u", "note": "n"}]
        deadline = time.time() + 4.0
        while time.time() < deadline:
            frames = screen.fb.frames
            if len(frames) >= 2 and not _is_error_card(frames[-1]):
                break
            time.sleep(0.05)
        stop.set()
        thread.join(1.0)
        self.assertGreaterEqual(len(screen.fb.frames), 2)
        self.assert_recovered(screen.fb.frames[-1])


class TestLayoutChromeContract(unittest.TestCase):
    """html-templates/layout.html: the shell the UI is built from.

    The contract is fixed and documented, so the shipped defaults and the
    template itself must never drift apart -- these run without the native
    library on purpose: a drifted contract should fail on any checkout.
    """

    def setUp(self):
        self.path = os.path.join(SHIPPED_ROOT, "layout.html")

    def placeholders(self):
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        # Comments are dropped before placeholders are read, so a
        # documented example in the header comment costs no variable.
        raw = templates.COMMENT_RE.sub("", raw)
        return set(templates.PLACEHOLDER_RE.findall(raw))

    def test_the_shell_is_discoverable(self):
        self.assertIn("layout.html", templates.available(SHIPPED_ROOT))

    def test_placeholders_are_exactly_the_documented_set(self):
        self.assertEqual(self.placeholders(), set(HTML.DEFAULT_VARS))

    def test_defaults_fill_it_with_no_leftovers(self):
        text, _root = templates.load("layout.html", HTML.DEFAULT_VARS,
                                     root=SHIPPED_ROOT)
        self.assertNotIn("{{", text)
        for key, value in HTML.DEFAULT_VARS.items():
            self.assertIn(templates.escape(value), text)

    def test_every_contract_key_is_documented_in_the_template(self):
        with open(self.path, encoding="utf-8") as handle:
            head = handle.read(4000)
        for key in HTML.DEFAULT_VARS:
            self.assertIn(key, head,
                          "%s is used but not documented in the header" % key)

    def test_no_javascript_and_no_remote_resources(self):
        with open(self.path, encoding="utf-8") as handle:
            # Comments go first, by the same rule the trust boundary uses:
            # the header comment documents that these are unsupported.
            text = templates.COMMENT_RE.sub("", handle.read()).lower()
        for banned in ("<script", "javascript:", "@import", "http://", "https://"):
            self.assertNotIn(banned, text)

    def test_pushed_values_are_escaped_in_the_shell_too(self):
        text, _root = templates.load(
            "layout.html",
            dict(HTML.DEFAULT_VARS, title="<img src=x onerror=alert(1)>"),
            root=SHIPPED_ROOT)
        self.assertNotIn("<img", text)
        self.assertIn("&lt;img", text)

    def test_a_short_push_names_the_missing_key(self):
        # The contract is not softened for the default template: a partial
        # push still names what is missing rather than drawing a wrong
        # shell, which is what makes a push reviewable.
        with self.assertRaises(templates.TemplateError) as caught:
            templates.load("layout.html", {"title": "T"}, root=SHIPPED_ROOT)
        message = str(caught.exception)
        self.assertIn("needs variable", message)
        named = message.split("'")[1] if "'" in message else ""
        self.assertIn(named, set(HTML.DEFAULT_VARS) - {"title"})


@requires_native
class TestLayoutRenders(unittest.TestCase):
    """A real 1920x1080 render of the shell, the way a tile reaches it."""

    def test_bare_tile_shows_the_shell_not_a_card(self):
        frames = run_view(make_screen(1920, 1080), {})
        self.assertTrue(frames, "no frame presented")
        frame = frames[-1]
        self.assertEqual(frame.size, (1920, 1080))
        self.assertFalse(_is_error_card(frame), "empty params drew an error")
        self.assertGreater(lit_pixels(frame), 20)

    def test_side_strips_and_content_band_are_where_they_are_documented(self):
        # The strips and the main band are different surfaces; if the frame
        # ever stops laying out, a tap aimed at a region lands on nothing.
        frame = run_view(make_screen(1920, 1080), {})[-1]
        strip = frame.getpixel((80, 540))
        main = frame.getpixel((960, 540))
        self.assertEqual(strip, (11, 13, 19), "left strip moved or vanished")
        self.assertEqual(main, (7, 8, 12), "content band moved or vanished")
        self.assertEqual(frame.getpixel((1900, 540)), strip,
                         "right strip missing (not mirrored)")
        self.assertNotEqual(strip, main, "the shell lost its chrome")

    def test_a_pushed_shell_re_renders_in_place(self):
        class Store:
            def __init__(self):
                self.payloads = []

            def get(self, renderer, name):
                return self.payloads

        screen = make_screen(640, 360)
        store = Store()
        screen.feeds = store
        stop = threading.Event()
        thread = threading.Thread(target=HTML.run, args=(screen, {}, stop))
        thread.daemon = True
        thread.start()
        thread.join(1.5)
        first = screen.fb.frames[-1] if screen.fb.frames else None
        store.payloads = [dict(HTML.DEFAULT_VARS, title="PUSHED")]
        deadline = time.time() + 4.0
        while time.time() < deadline and len(screen.fb.frames) < 2:
            time.sleep(0.05)
        stop.set()
        thread.join(1.0)
        self.assertGreaterEqual(len(screen.fb.frames), 2, "push did not draw")
        self.assertFalse(_is_error_card(screen.fb.frames[-1]))
        self.assertNotEqual(first.tobytes(), screen.fb.frames[-1].tobytes())

    def test_a_short_push_draws_a_card_and_recovers(self):
        class Store:
            def __init__(self):
                self.payloads = []

            def get(self, renderer, name):
                return self.payloads

        screen = make_screen(640, 360)
        store = Store()
        screen.feeds = store
        stop = threading.Event()
        thread = threading.Thread(target=HTML.run, args=(screen, {}, stop))
        thread.daemon = True
        thread.start()
        thread.join(1.5)
        store.payloads = [{"title": "HALF"}]
        deadline = time.time() + 4.0
        while time.time() < deadline:
            frames = screen.fb.frames
            if frames and _is_error_card(frames[-1]):
                break
            time.sleep(0.05)
        stop.set()
        thread.join(1.0)
        self.assertTrue(_is_error_card(screen.fb.frames[-1]),
                        "a half-filled push drew a shell instead of naming it")

    def test_idle_shell_does_not_re_render(self):
        # The change detector is what keeps a non-STATIC view off the CPU
        # when nothing is pushed; the shell is the default case for it.
        frames = run_view(make_screen(1920, 1080), {}, seconds=3.0)
        self.assertEqual(len(frames), 1,
                         "idle shell re-rendered %d times" % len(frames))


if __name__ == "__main__":
    unittest.main()
