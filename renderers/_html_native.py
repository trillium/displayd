"""Python half of the displayd html renderer: the PIL painter and the render
entry point. The ctypes binding, fonts, images and geometry live in the
_html_* siblings and are re-exported here.

litehtml does the parsing and layout; everything that puts a pixel down
happens here, through PIL, exactly like every other renderer in this repo.
The native side (``renderers/native/``) never sees a PIL object, and this
side never sees a litehtml object -- the two only exchange the plain C
structs in ``displayd_html.h``.

Trust boundary: the parser is not a sandbox. This module refuses to open
anything outside the template root handed to it (:func:`allow_root`), and
refuses URLs entirely, so a document can only ever read local files an
operator put next to the template. No socket is ever created.

Build: ``tools/build_litehtml.sh``. Missing library is a hard, explicit
error -- never a silent no-op frame.
"""

import ctypes
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Re-exports: the names callers and tests reach through ``_html_native``.
from _html_abi import (CTX, DOUBLE, ERRLEN, LHTML_OK,  # noqa: E402,F401
                       RADII, REPEAT_NO_REPEAT, REPEAT_REPEAT,
                       REPEAT_REPEAT_X, REPEAT_REPEAT_Y, SIDE_BOTTOM,
                       SIDE_LEFT, SIDE_RIGHT, SIDE_TOP, SIDES, HtmlRenderError,
                       NativeMissing, _BorderSide, _Callbacks, _Color,
                       _FontDesc, _FontMetrics, _Radii, _Rect)
import _html_abi  # noqa: E402
from _html_fonts import (FONT_CANDIDATES, MAX_FONTS, _FontCache,  # noqa: E402,F401
                         _FontEntry, _font_file, _make_font, ui_font)
from _html_geom import (_border_ring, _clip_box, _cut, _radii, _rgba,  # noqa: E402,F401
                        _shape_for, _visible)
from _html_images import (MAX_IMAGE_PIXELS, MAX_IMAGES, _REPEATS_X,  # noqa: E402,F401
                          _REPEATS_Y, _combine, _compose_repeat,
                          _ImageCache, _paste_clipped, allow_root,
                          resolve_local)
import _html_trampolines as trampolines  # noqa: E402

NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
LIB_NAMES = ("liblitehtmlpil.dylib", "liblitehtmlpil.so")


def lib_path():
    """The engine library the renderer would load, or None when unbuilt.

    Searched in one place, in one order, so the installer
    (tools/install_html_runtime.sh) can check the same contract the load
    path uses: ``$DISPLAYD_HTML_LIB`` first, then ``renderers/native/``
    beside this module. That second one is an installed-tree path derived
    from ``__file__``, so an installed target needs no environment variable
    and no developer-local checkout to find its runtime.
    """
    override = os.environ.get("DISPLAYD_HTML_LIB")
    if override:
        return override
    for name in LIB_NAMES:
        path = os.path.join(NATIVE_DIR, name)
        if os.path.exists(path):
            return path
    return None


def lib():
    """Load (once) the native layout engine, or raise with the fix in it."""
    return _html_abi.load(lib_path(), NATIVE_DIR)


def version():
    """Upstream revision + ABI string, for /state and bug reports."""
    return lib().lhtml_version().decode("utf-8", "replace")


# ----------------------------------------------------------------- painter


class _Painter:
    """Draws one frame. Owned by a single render call; dies with it."""

    def __init__(self, width, height, background, root):
        self.width = width
        self.height = height
        self.root = allow_root(root) if root else None
        self.img = Image.new("RGB", (max(1, width), max(1, height)),
                             tuple(background[:3]))
        self.draw = ImageDraw.Draw(self.img)
        self.fonts = _FontCache()
        self.images = _ImageCache()

    # -- fonts ----------------------------------------------------------
    def create_font(self, descr, out):
        entry = self.fonts.create(descr)
        if entry is None or not entry.handle:
            return
        out.id = entry.handle
        out.height = float(entry.ascent + entry.descent)
        out.ascent = float(entry.ascent)
        out.descent = float(entry.descent)
        out.x_height = float(entry.ascent) * 0.5
        out.ch_width = float(entry.ch_width)

    def delete_font(self, handle):
        self.fonts.release(handle)

    def text_width(self, handle, text):
        return self.fonts.width(handle, text.decode("utf-8", "replace"))

    # -- shapes ---------------------------------------------------------
    def fill(self, box, clip, color, shape=None):
        """Paint `box` in `color`.

        `shape` is an "L" coverage mask the size of the visible box, or None
        for the square path. None keeps the original code exactly: one
        ImageDraw.rectangle for opaque ink, one alpha paste for translucent.
        With a mask the colour is put down through that mask instead, so a
        partial corner pixel is a blend rather than a hole or a hard step.
        The mask is resolved by the caller, before this runs, which is what
        keeps the no-partial-frame promise: either we hold a whole mask or
        we take the untouched square path, and nothing is ever drawn twice.
        """
        area = _visible(box, clip)
        if not area or color.a == 0:
            return
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        if shape is not None and shape.size != (w, h):
            # A mask that does not line up with the pixels it would cover
            # is not usable, and resizing it would smear the curve. Take the
            # square path: a shape with square corners is a lesser wrong
            # than a shape pasted at the wrong offset.
            shape = None
        if shape is None:
            if color.a >= 255:
                self.draw.rectangle([x0, y0, x1 - 1, y1 - 1],
                                    fill=(color.r, color.g, color.b))
                return
            shape = Image.new("L", (w, h), color.a)
        elif color.a < 255:
            shape = shape.point(lambda v: v * color.a // 255)
        self.img.paste(Image.new("RGB", (w, h), (color.r, color.g, color.b)),
                       (x0, y0), shape)

    def borders(self, box, clip, sides, radii=None):
        """Trapezoid-accurate borders are out of scope: solid sides only.

        litehtml hands over the resolved box, so each visible solid side is
        one rectangle. Dashed, double, and 3D styles fall back to solid --
        visible, never wrong-shaped. The side order is the C ABI's, see the
        LHTML_SIDE_* macros in displayd_html.h.

        With radii the sides stop being rectangles and become pieces of the
        border RING (see _border_ring): each side rectangle intersected with
        outer-minus-inner. That is what gives the corners to the sides that
        reach them, and it still puts a per-side colour on its own edge,
        which a rounded rectangle drawn per side could not do.
        """
        x0, y0, x1, y1 = box
        edges = ((sides[SIDE_LEFT], x0, y0, x0 + sides[SIDE_LEFT].width, y1),
                 (sides[SIDE_TOP], x0, y0, x1, y0 + sides[SIDE_TOP].width),
                 (sides[SIDE_RIGHT], x1 - sides[SIDE_RIGHT].width, y0, x1, y1),
                 (sides[SIDE_BOTTOM], x0, y1 - sides[SIDE_BOTTOM].width,
                  x1, y1))
        ring = None
        if radii is not None:
            ring, origin = _border_ring(box, sides, radii)
        for side, sx0, sy0, sx1, sy1 in edges:
            if side.width <= 0 or side.color.a == 0:
                continue
            area = _visible((sx0, sy0, sx1, sy1), clip)
            if not area:
                continue
            # Clip FIRST, then cut the mask to the same region, so the two
            # are always the same size: a partly clipped border ring still
            # lands on exactly the pixels it covers.
            self.fill(area, clip, side.color,
                      None if ring is None else _cut(ring, area, origin))

    # -- text -----------------------------------------------------------
    def text(self, handle, raw, color, box, clip):
        entry = self.fonts.font(handle)
        if entry is None or not raw:
            return
        value = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if not value.strip(" "):
            return
        area = _visible(box, clip)
        if not area:
            return
        x, y = int(round(box[0])), int(round(box[1]))
        baseline = y + entry.ascent
        ink = (color.r, color.g, color.b)
        if color.a >= 255:
            self.draw.text((x, baseline), value, font=entry.image_font,
                           fill=ink, anchor="ls")
            return
        # Translucent ink needs a coverage mask: PIL on an RGB canvas drops
        # alpha, so draw the glyphs' coverage then blend the ink through it.
        w = max(1, int(entry.width(value)))
        h = max(1, entry.ascent + entry.descent)
        mask = Image.new("L", (w + 4, h + 4), 0)
        ImageDraw.Draw(mask).text((2, entry.ascent), value,
                                 font=entry.image_font, fill=color.a,
                                 anchor="ls")
        self.img.paste(Image.new("RGB", mask.size, ink), (x - 2, y), mask)

    # -- images ---------------------------------------------------------
    def image_size(self, url, base_url, width, height):
        w, h = self.images.size(url, base_url, self.root)
        width[0] = w
        height[0] = h

    def image(self, url, base_url, box, area, clip, repeat):
        self.images.draw(self.img, url, base_url, box, area, clip, repeat,
                         self.root)


def render(document, width, height, background=(0, 0, 0), root=None):
    """Lay out and paint `document`, returning (image, content_height).

    `root` is the trusted local directory a document may reference images
    from; pass None to refuse every image reference. Raises
    HtmlRenderError with a display-facing message on any native failure --
    a bad document must never reach screen.present half-drawn.
    """
    library = lib()
    painter = _Painter(width, height, background, root)
    ctx = trampolines.register(painter)
    try:
        callbacks = trampolines.callbacks(ctx)
        content_height = ctypes.c_double(0.0)
        err = ctypes.create_string_buffer(ERRLEN)
        code = library.lhtml_render(
            ctypes.byref(callbacks), document.encode("utf-8", "replace"),
            int(width), int(height), ctypes.byref(content_height), err, ERRLEN)
        if code != LHTML_OK:
            raise HtmlRenderError(err.value.decode("utf-8", "replace").strip()
                                  or "html render failed (code %d)" % code)
        return painter.img, content_height.value
    finally:
        trampolines.unregister(ctx)
