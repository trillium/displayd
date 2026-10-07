"""Tests for border-radius end to end: engine -> C ABI -> PIL painter.

Why these are pixel assertions and not geometry assertions: the whole point
of the change is that a rounded corner covers FEWER pixels than a square one
and that the four corners stay independent. A call into the mask builder
would pass just as happily if nothing downstream ever used it -- the bug
being pinned here is a shape that renders square, which only a rendered
frame can show.

The failure mode this locks down, from the evidence report: litehtml parsed
and resolved `border-radius` perfectly for years and handed the eight
per-corner values to `pil_container`, which dropped them, and the C ABI had
nowhere to put them even once it did not. So:

  - before the change every test here fails, because every corner paints;
  - after it, `TestRoundedShape` sees uncovered corners, `TestSquareFallback`
    sees byte-identical output to the pre-change path, and
    `TestCornersStayIndependent` sees four different corners from four
    different declared radii.

Run from the repo root:  python3 -m unittest tests.test_html_radius -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "renderers"))

from PIL import Image  # noqa: E402

import _html_native as native  # noqa: E402
from ui import radius  # noqa: E402

W, H = 300, 140
BOX_W, BOX_H = 260, 100
INK = (32, 64, 128)


def native_built():
    return any(os.path.exists(os.path.join(native.NATIVE_DIR, name))
               for name in native.LIB_NAMES)


requires_native = unittest.skipUnless(
    native_built(), "native library not built (tools/build_litehtml.sh)")


def box(**css):
    """A full-canvas div with the given declarations, margin reset."""
    style = ";".join("%s:%s" % kv for kv in sorted(css.items()))
    return ("<style>*{margin:0;padding:0}</style>"
            "<div style='width:%dpx;height:%dpx;background:#204080;%s'>"
            "</div>" % (BOX_W, BOX_H, style))


def render(document, w=W, h=H):
    image, _ = native.render(document, w, h)
    return image


def corners(image, inset=6):
    """The four near-corner pixels, clockwise from the top-left."""
    return (image.getpixel((inset, inset)),
            image.getpixel((BOX_W - 1 - inset, inset)),
            image.getpixel((inset, BOX_H - 1 - inset)),
            image.getpixel((BOX_W - 1 - inset, BOX_H - 1 - inset)))


def inked(image):
    """Bytes of the box colour actually on the frame.

    Counting painted bytes rather than "is this pixel the colour" is what
    makes an anti-aliased edge measurable: a soft corner contributes a
    partial blend, which a single getpixel would read as "not painted" and a
    pixel COUNT reads as less area. Both directions of the assertion need
    the count, so this is the primitive the class is built on.
    """
    data = image.tobytes()
    return sum(1 for i in range(0, len(data), 3)
               if data[i] or data[i + 1] or data[i + 2])


class TestRoundedShape(unittest.TestCase):
    """A declared radius must actually remove corner coverage."""

    @requires_native
    def test_uniform_radius_leaves_the_four_corners_unpainted(self):
        square = render(box(**{"border-radius": "0"}))
        rounded = render(box(**{"border-radius": "40px"}))
        self.assertEqual(corners(square), (INK,) * 4,
                         "baseline: a square div paints all four corners")
        self.assertEqual(corners(rounded), ((0, 0, 0),) * 4,
                         "a 40px radius must uncover all four corners")
        self.assertLess(inked(rounded), inked(square),
                        "rounded must cover strictly fewer pixels than square")

    @requires_native
    def test_the_corner_edge_is_anti_aliased_not_a_staircase(self):
        # A hard-edged rounded corner has exactly two values in the corner
        # region: the ink and the canvas. Anti-aliasing is what puts the
        # levels between them in -- this is the assertion that would fail if
        # someone swapped the supersampled mask for ImageDraw's own
        # rounded_rectangle, which is hard-edged by construction.
        image = render(box(**{"border-radius": "40px"}))
        shades = {image.getpixel((x, x)) for x in range(40)}
        self.assertGreater(len(shades), 2,
                           "corner must blend, not step: %r" % sorted(shades))
        self.assertIn((0, 0, 0), shades, "the corner still has to be open")
        self.assertIn(INK, shades, "the straight run still has to be solid")

    @requires_native
    def test_a_border_ring_rounds_and_keeps_its_own_geometry(self):
        sides = {"border-width": "6px", "border-style": "solid",
                 "border-color": "#ffffff"}
        square = render(box(**dict(sides, **{"border-radius": "0"})))
        rounded = render(box(**dict(sides, **{"border-radius": "18px"})))
        white = (255, 255, 255)
        self.assertEqual(square.getpixel((0, 0)), white,
                         "an unrounded ring owns its own corner pixel")
        self.assertEqual(rounded.getpixel((0, 0)), (0, 0, 0),
                         "a rounded ring does not")
        for edge in ((BOX_W // 2, 0), (0, BOX_H // 2)):
            self.assertEqual(rounded.getpixel(edge), white,
                             "the straight runs must survive rounding")
        self.assertEqual(rounded.getpixel((BOX_W // 2, BOX_H // 2)), INK,
                         "the interior is still the background")

    @requires_native
    def test_elliptical_radii_round_only_the_corners_that_reach(self):
        # 60px 10px / 20px 40px is CSS's slash form: horizontal radii
        # 60/10/60/10, vertical 20/40/20/40. The two wide corners lose their
        # corner pixel at 6px in; the two 10px-wide ones are already solid
        # ink by then. A uniform radius could not produce that pattern.
        image = render(box(**{"border-radius": "60px 10px / 20px 40px"}))
        self.assertEqual(corners(image), ((0, 0, 0), INK, INK, (0, 0, 0)),
                         "only the 60px-wide corners should be cut")


class TestCornersStayIndependent(unittest.TestCase):
    """Eight independent radii must survive as eight, not collapse to one."""

    @requires_native
    def test_per_corner_longhands_round_only_the_corners_named(self):
        image = render(box(**{"border-top-left-radius": "70px",
                              "border-bottom-right-radius": "35px"}))
        self.assertEqual(corners(image),
                         ((0, 0, 0), INK, INK, (0, 0, 0)),
                         "top-left and bottom-right round; the others do not")

    @requires_native
    def test_four_different_corners_stay_four_different_corners(self):
        # The strongest form of "per corner, not one uniform value": four
        # radii that are all different, two of them elliptical, so no single
        # number can reproduce the result. At 2px in, the 60px top-left and
        # the 80px bottom-right are wide open, the 2px bottom-left is still
        # solid ink, and the 4px top-right is NEITHER -- it is a partial
        # blend, which is only reachable if its own radius is neither the
        # largest nor the smallest. Three distinct outcomes from four
        # declared radii.
        image = render(box(**{"border-radius":
                              "60px 4px 80px 2px / 8px 30px 20px 2px"}))
        tl, tr, bl, br = corners(image, 2)
        self.assertEqual(tl, (0, 0, 0), "60px wide: fully open")
        self.assertEqual(bl, INK, "2px: solid ink this close in")
        self.assertEqual(br, (0, 0, 0), "80px wide: fully open")
        self.assertNotIn(tr, (INK, (0, 0, 0)),
                         "4px must be a partial blend, not either extreme")
        self.assertNotEqual(tr, tl, "a shared value would mean one radius")

    @requires_native
    def test_a_percentage_resolves_against_each_axis_separately(self):
        # 25% of a 260x100 border box is 65 across and 25 down, which is an
        # ellipse. `65px / 25px` says the same thing in px, so the two must
        # render identically -- that is what pins per-axis percentage
        # resolution. It must also differ from a disc, because a 65px
        # radius is not a thing: litehtml clamps it to half the 100px box.
        percent = render(box(**{"border-radius": "25%"}))
        explicit = render(box(**{"border-radius": "65px / 25px"}))
        disc = render(box(**{"border-radius": "65px"}))
        self.assertEqual(percent.tobytes(), explicit.tobytes(),
                         "25% and 65px/25px are the same shape")
        self.assertNotEqual(percent.tobytes(), disc.tobytes(),
                            "25% is a 65x25 ellipse, not the clamped disc")
        self.assertEqual(corners(percent), ((0, 0, 0),) * 4)


class TestSquareFallback(unittest.TestCase):
    """Absent or malformed radii must render exactly as before."""

    @requires_native
    def test_no_radius_is_byte_identical_to_the_square_declaration(self):
        plain = render(box())
        zero = render(box(**{"border-radius": "0px"}))
        self.assertEqual(plain.tobytes(), zero.tobytes())

    @requires_native
    def test_a_malformed_radius_renders_exactly_square(self):
        # litehtml rejects these at parse time, so the container never even
        # offers a radius. The point is that the outcome is the documented
        # square pixels and not a dropped frame.
        square = render(box(**{"border-radius": "0px"}))
        for bad in ("banana", "-", "10 px", "1e999px", "calc(4px)", "1/2"):
            with self.subTest(border_radius=bad):
                image = render(box(**{"border-radius": bad}))
                self.assertEqual(image.tobytes(), square.tobytes(),
                                 "%r must not change a single pixel" % bad)

    def test_degenerate_radii_never_reach_the_mask(self):
        # The painter's own guard, without the engine: a NULL pointer, an
        # all-zero struct, a negative and a NaN all mean "square", so a
        # container that sent junk cannot make the painter draw half a shape.
        self.assertIsNone(native._radii(None))
        blank = native._Radii()
        self.assertIsNone(native._radii(native.ctypes.pointer(blank)))
        blank.top_left_x = -8.0
        self.assertIsNone(native._radii(native.ctypes.pointer(blank)))
        blank.top_left_x = float("nan")
        self.assertIsNone(native._radii(native.ctypes.pointer(blank)))
        blank.top_left_x = 12.0
        self.assertEqual(len(native._radii(native.ctypes.pointer(blank))), 8)

    @requires_native
    def test_a_radius_change_never_leaves_a_partial_frame(self):
        # The no-blank-frame guarantee, exercised the only way that means
        # anything: every frame is fully painted or the render fails. The
        # masks are resolved before any pixel moves and a frame is never
        # presented twice, so a radius change must simply swap one whole
        # shape for another.
        seen = []
        for radius_value in ("0px", "40px", "0px", "12px", "40px", "0px"):
            image = render(box(**{"border-radius": radius_value}))
            self.assertEqual(image.size, (W, H))
            self.assertEqual(image.mode, "RGB")
            painted = inked(image)
            self.assertGreater(painted, 0, "a frame may never be blank")
            seen.append(painted)
        # Same radius, same pixels -- which is what makes the mask cache safe
        # to hold between frames rather than a source of drift.
        self.assertEqual(seen[0], seen[2])
        self.assertEqual(seen[1], seen[4])
        self.assertNotEqual(seen[0], seen[1])


class TestMaskCache(unittest.TestCase):
    """The cache and the shapes, away from the engine."""

    def setUp(self):
        radius.clear()
        self.addCleanup(radius.clear)

    def test_a_square_mask_is_uniformly_opaque(self):
        # This is what makes an unrounded box indistinguishable from the old
        # ImageDraw.rectangle: every pixel is 255, so pasting through the
        # mask cannot darken an edge.
        mask = radius.rounded(60, 40, (0,) * 8)
        self.assertEqual(set(mask.tobytes()), {255})

    def test_only_the_corner_quadrants_differ_from_a_square(self):
        mask = radius.rounded(100, 60, (20, 20, 20, 20, 20, 20, 20, 20))
        self.assertEqual(mask.getpixel((0, 30)), 255, "left edge stays solid")
        self.assertEqual(mask.getpixel((50, 0)), 255, "top edge stays solid")
        self.assertEqual(mask.getpixel((0, 0)), 0, "corner is open")

    def test_the_mask_area_matches_the_analytic_value(self):
        # A uniform radius r removes 4*(r^2 - pi*r^2/4). Measured on the
        # coverage values rather than on the shape's bounding box, this is
        # the assertion that the anti-aliasing is CORRECT and not merely
        # present: a mask that rounded in the right places but over- or
        # under-shot the curve would still pass every corner test above.
        import math
        w, h, r = 300, 120, 40
        mask = radius.rounded(w, h, (r,) * 8)
        coverage = sum(mask.tobytes()) / 255.0
        self.assertAlmostEqual(coverage, w * h - (4 - math.pi) * r * r,
                               delta=w * h * 0.002)

    def test_radii_are_cached_on_size_and_shape_alone(self):
        first = radius.shape(80, 60, (12,) * 8)
        again = radius.shape(80, 60, (12,) * 8)
        self.assertIs(first, again, "same key must reuse the same mask")
        self.assertIsNot(first, radius.shape(80, 60, (13,) * 8))
        self.assertIsNot(first, radius.shape(81, 60, (12,) * 8))
        self.assertEqual(radius.live(), 3)

    def test_the_cache_is_bounded(self):
        for n in range(radius.MAX_MASKS * 3):
            radius.shape(40 + n, 30, (4,) * 8)
        self.assertLessEqual(radius.live(), radius.MAX_MASKS)

    def test_radii_are_clamped_so_the_corners_cannot_overlap(self):
        # At exactly half the box the four arc quadrants touch, and which
        # corner won a shared pixel would depend on paste order.
        mask = radius.rounded(40, 40, (20, 20, 20, 20, 20, 20, 20, 20))
        self.assertEqual(mask.getpixel((0, 0)), 0)
        self.assertEqual(mask.getpixel((20, 20)), 255)

    def test_degenerate_sizes_do_not_raise(self):
        for size in ((1, 1), (1, 40), (40, 1)):
            mask = radius.rounded(size[0], size[1], (20,) * 8)
            self.assertEqual(mask.size, size)

    def test_the_module_is_inside_the_line_budget(self):
        path = os.path.abspath(radius.__file__)
        with open(path) as handle:
            self.assertLessEqual(len(handle.read().splitlines()), 250)


class TestTemplateDeclarations(unittest.TestCase):
    """The three shipped surfaces, measured through the real render path.

    A `border-radius` in a file is only a claim. These go through the same
    load -> litehtml -> painter chain the home screen uses, at the size each
    template is authored at, and read pixels -- so a declaration litehtml
    drops, or a radius the container forgets to forward, fails here.
    """

    def setUp(self):
        self.templates = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "html-templates")

    def read(self, name):
        with open(os.path.join(self.templates, name)) as handle:
            return handle.read()

    def test_the_three_surfaces_declare_a_radius(self):
        dock = self.read("dock.html")
        chrome = self.read("_chrome.html")
        self.assertIn("border-radius: 28px", dock)      # the strip outline
        self.assertIn("border-radius: 50%", dock)        # the head dot
        self.assertIn("border-radius: 24px", chrome)     # the tile frame

    def test_no_template_still_claims_radii_do_not_arrive(self):
        # The residue of the old limitation. If a future edit reintroduces
        # "square, not rounded" here, the notes and the pixels disagree.
        for name in ("dock.html", "_chrome.html"):
            body = self.read(name)
            for stale in ("corners are square", "paints square",
                          "ignores them", "not rounded"):
                self.assertNotIn(stale, body,
                                 "%s still claims %r" % (name, stale))

    @requires_native
    def test_the_loaded_dock_template_really_renders_rounded(self):
        import _html_templates as templates
        import unified_dock
        document, _root = templates.load(
            "dock.html", unified_dock.dock_variables(None, False))
        w, h = unified_dock.DESIGN
        image = render(document, w, h)
        background = (8, 10, 16)   # the dock's own bg, from dock_variables

        # 1. The strip outline: all four corners cut, straight runs intact.
        for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            self.assertEqual(image.getpixel(corner), background,
                             "28px outline corner %r must be cut" % (corner,))
        self.assertNotEqual(image.getpixel((w // 2, 1)), background,
                            "the top run of the outline must survive")

        # 2. The head dot: found by its colour inside the head row's left
        # corner, then measured. The tap hint on the tail row is the same
        # accent, so the search is bounded to the head row -- and the brand
        # text beside the dot is white, so the dot is the only accent there.
        # A 50% radius is a circle: the exact corner pixel of the dot's own
        # bounding box is background, its widest row is 20 and its top row is
        # a few pixels. A 20px SQUARE dot, which is what this was before,
        # fails all three.
        accent = unified_dock.theme.rgb("dock")
        head = [(x, y) for y in range(0, 70) for x in range(0, 120)
                if image.getpixel((x, y)) == accent]
        self.assertTrue(head, "the live accent dot must be on the strip")
        xs = [p[0] for p in head]
        ys = [p[1] for p in head]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        self.assertEqual((right - left + 1, bottom - top + 1), (20, 20),
                         "the head dot is a 20px box")
        self.assertEqual(image.getpixel((left, top)), background,
                         "a 50%% radius is a circle: its corner is cut")
        widths = [sum(1 for x in range(left, right + 1)
                      if image.getpixel((x, y)) == accent)
                  for y in range(top, bottom + 1)]
        self.assertEqual(max(widths), 20, "the circle's widest row is 20")
        self.assertLess(min(widths), 6,
                        "its top row must be a few pixels, not 20")

    @requires_native
    def test_a_composed_tile_frame_is_rounded(self):
        # The tile radius lives in _chrome.html, which no template loads on
        # its own: it is spliced in at load time. So the only assertion worth
        # making goes through the real composition path, at the real tile
        # geometry the picker hands it.
        import picker
        from ui import tile as ui_tile

        class Screen:
            W, H = 1920, 1080

            def new_image(self, bg):
                return Image.new("RGB", (self.W, self.H), tuple(bg))

        screen = Screen()
        views = ["clock", "chat", "row", "stream", "options", "reload"]
        fill, page = (140, 160, 190), (8, 10, 16)
        rect = picker.coerce_rect({}, screen.W, screen.H)
        geometry = picker.grid_geometry(rect, len(views))
        image = picker.draw(screen, views, geometry, rect, [fill], page,
                            (255, 255, 255), fill)
        x, y, w, h = geometry[0]

        # The tile frame is every pixel in the tile's own box that is neither
        # its fill nor the page behind it.
        frame = [(xx, yy) for yy in range(y, y + h) for xx in range(x, x + w)
                 if image.getpixel((xx, yy)) not in (fill, page)]
        self.assertTrue(frame, "the tile frame must actually draw")
        xs = [p[0] for p in frame]
        ys = [p[1] for p in frame]
        self.assertEqual((min(xs), min(ys), max(xs), max(ys)), (x, y, x + w - 1, y + h - 1),
                         "the frame owns the whole declared tile box")
        # 24px radius: the frame's straight runs survive, its outer corner
        # does not reach the corner of the box.
        ink = image.getpixel((x + w // 2, y))
        self.assertNotIn(ink, (fill, page),
                         "the top run of the frame must survive")
        self.assertEqual(image.getpixel((x, y + h // 2)), ink,
                         "and the left run must be the same frame ink")
        self.assertEqual(image.getpixel((x, y)), page,
                         "24px radius: the outer corner must be cut")
        self.assertEqual(image.getpixel((x + w - 1, y + h - 1)), page,
                         "and so must the opposite one")


if __name__ == "__main__":
    unittest.main()
